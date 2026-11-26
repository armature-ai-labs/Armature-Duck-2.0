# -*- coding: utf-8 -*-
"""Minimal Feetech STS/SMS protocol implementation: framing, read/write, sync I/O, reboot."""
import struct
import threading
import time

PING, READ, WRITE, REG_WRITE, ACTION = 0x01, 0x02, 0x03, 0x04, 0x05
RECOVERY, REBOOT, BACKUP, RESET_TURNS, CALIBRATE = 0x06, 0x08, 0x09, 0x0A, 0x0B
SYNC_READ, SYNC_WRITE = 0x82, 0x83
INSTR_NAMES = {0x01: "PING", 0x02: "READ", 0x03: "WRITE", 0x04: "REG_WRITE", 0x05: "ACTION",
               0x06: "RECOVERY", 0x08: "REBOOT", 0x09: "BACKUP", 0x0A: "RESET_TURNS", 0x0B: "CALIBRATE",
               0x82: "SYNC_READ", 0x83: "SYNC_WRITE"}
BROADCAST = 0xFE

# Memory table: address -> (name, size_bytes, area) for register dump.
REGISTERS = {
    0: ("Firmware Major", 1, "RO"), 1: ("Firmware Minor", 1, "RO"), 2: ("END Endian (0=LE)", 1, "RO"),
    3: ("Servo Major", 1, "RO"), 4: ("Servo Minor", 1, "RO"),
    5: ("ID", 1, "EEPROM"), 6: ("Baud Rate (0=1M)", 1, "EEPROM"), 7: ("Reserved", 1, "EEPROM"),
    8: ("Response Level", 1, "EEPROM"), 9: ("Min Angle Limit", 2, "EEPROM"), 11: ("Max Angle Limit", 2, "EEPROM"),
    13: ("Max Temp Limit", 1, "EEPROM"), 14: ("Max Input Voltage", 1, "EEPROM"), 15: ("Min Input Voltage", 1, "EEPROM"),
    16: ("Max Torque", 2, "EEPROM"), 18: ("Phase", 1, "EEPROM"), 19: ("Unload Condition", 1, "EEPROM"),
    20: ("LED Alarm Condition", 1, "EEPROM"), 21: ("Position Kp", 1, "EEPROM"), 22: ("Position Kd", 1, "EEPROM"),
    23: ("Position Ki", 1, "EEPROM"), 24: ("Min Startup Force", 1, "EEPROM"), 25: ("Integral Limit", 1, "EEPROM"),
    26: ("CW Deadband", 1, "EEPROM"), 27: ("CCW Deadband", 1, "EEPROM"), 28: ("Protection Current", 2, "EEPROM"),
    30: ("Angle Resolution", 1, "EEPROM"), 31: ("Position Offset", 2, "EEPROM"), 33: ("Operating Mode", 1, "EEPROM"),
    34: ("Hold Torque", 1, "EEPROM"), 35: ("Protection Time", 1, "EEPROM"), 36: ("Overload Torque", 1, "EEPROM"),
    37: ("Velocity Kp", 1, "EEPROM"), 38: ("Overcurrent Time", 1, "EEPROM"), 39: ("Velocity Ki", 1, "EEPROM"),
    40: ("Torque Switch", 1, "SRAM"), 41: ("Acceleration", 1, "SRAM"), 42: ("Goal Position", 2, "SRAM"),
    44: ("PWM Open-Loop Speed", 2, "SRAM"), 46: ("Running Speed", 2, "SRAM"), 48: ("Torque Limit", 2, "SRAM"),
    50: ("Running Kp", 1, "SRAM"), 51: ("Running Kd", 1, "SRAM"), 52: ("Running Ki", 1, "SRAM"),
    55: ("Lock Flag", 1, "SRAM"),
    56: ("Present Position", 2, "Feedback"), 58: ("Present Speed", 2, "Feedback"), 60: ("Present Load", 2, "Feedback"),
    62: ("Present Voltage (0.1V)", 1, "Feedback"), 63: ("Present Temp (C)", 1, "Feedback"), 64: ("Async Write Flag", 1, "Feedback"),
    65: ("Servo Status", 1, "Feedback"), 66: ("Moving Flag", 1, "Feedback"), 67: ("Goal Position (Readback)", 2, "Feedback"),
    69: ("Present Current (6.5mA)", 2, "Feedback"),
    80: ("Moving Speed Threshold", 1, "Factory"), 81: ("DTs (ms)", 1, "Factory"), 82: ("Speed Unit Factor", 1, "Factory"),
    83: ("Min Speed Limit", 1, "Factory"), 84: ("Max Speed Limit", 1, "Factory"), 85: ("Accel Limit", 1, "Factory"),
    86: ("Accel Multiplier", 1, "Factory"),
}
STATUS_BITS = {0: "Voltage", 1: "Encoder", 2: "Temp", 3: "Current", 5: "Load"}
DUMP_END = max(REGISTERS) + 1


def sign15(v):
    """Signed 16-bit value with BIT15 sign bit (position, speed, current)."""
    return -(v & 0x7FFF) if v & 0x8000 else v


def sign10(v):
    """Signed load value with BIT10 sign bit."""
    return -(v & 0x3FF) if v & 0x400 else v


class BusError(Exception):
    pass


class BusTimeout(BusError):
    """No reply or incomplete reply from servo."""


def hexs(b):
    return " ".join(f"{x:02x}" for x in b)


class FeetechBus:
    def __init__(self, port, baud=1_000_000, timeout=0.02, ser=None):
        if ser is None:
            import serial   # noqa: PLC0415
            ser = serial.Serial(port, baud, timeout=timeout, write_timeout=0.1)
        self.ser = ser
        self._io = threading.Lock()
        self.trace = None
        self.trace_skip = {SYNC_READ, SYNC_WRITE}
        self._last = (None, None)
        self.stats = {"tx": 0, "rx_ok": 0, "timeout": 0, "bad_checksum": 0, "bad_id": 0}
        self.per_id = {}

    def _note(self, sid, ok, err=0):
        c = self.per_id.get(sid)
        if c is None:
            c = self.per_id[sid] = {"asked": 0, "ok": 0, "missed": 0, "alarm": 0, "streak": 0, "max_streak": 0}
        c["asked"] += 1
        if ok:
            c["ok"] += 1
            c["streak"] = 0
            if err:
                c["alarm"] += 1
        else:
            c["missed"] += 1
            c["streak"] += 1
            c["max_streak"] = max(c["max_streak"], c["streak"])

    def close(self):
        self.ser.close()

    # ---- Packets ----
    @staticmethod
    def _packet(sid, instr, params=b""):
        length = len(params) + 2
        chk = (~(sid + length + instr + sum(params))) & 0xFF
        return bytes([0xFF, 0xFF, sid, length, instr]) + bytes(params) + bytes([chk])

    def _send(self, sid, instr, params=b""):
        self.ser.reset_input_buffer()
        pkt = self._packet(sid, instr, params)
        self.ser.write(pkt)
        self.stats["tx"] += 1
        self._last = (sid, instr)
        if self.trace and instr not in self.trace_skip:
            note = ""
            if instr in (READ, WRITE) and params:
                reg = REGISTERS.get(params[0])
                note = f"  addr {params[0]}" + (f" [{reg[0]}]" if reg else "") + (
                    f" write {list(params[1:])}" if instr == WRITE else f" read {params[1]}B")
            self.trace(f"→ #{sid} {INSTR_NAMES.get(instr, hex(instr))} {hexs(pkt)}{note}")

    def _read_status(self, expect_id=None):
        """Read one status packet and return (id, error, params)."""
        hdr = b""
        deadline = time.monotonic() + self.ser.timeout * 3
        while time.monotonic() < deadline:
            b = self.ser.read(1)
            if not b:
                break
            hdr = (hdr + b)[-2:]
            if hdr == b"\xff\xff":
                break
        who = f"id {expect_id}" if expect_id is not None else "servo"
        if hdr != b"\xff\xff":
            self.stats["timeout"] += 1
            raise BusTimeout(f"{who} timed out (no header within {self.ser.timeout * 3 * 1000:.0f} ms)")
        rest = self.ser.read(2)
        if len(rest) < 2:
            self.stats["timeout"] += 1
            raise BusTimeout(f"{who} truncated ID/length (got ff ff {hexs(rest)})")
        sid, length = rest[0], rest[1]
        body = self.ser.read(length)
        if len(body) < length:
            self.stats["timeout"] += 1
            raise BusTimeout(f"{who} incomplete reply: expected {length}, got {len(body)} (ff ff {hexs(rest + body)})")
        err, params, chk = body[0], body[1:-1], body[-1]
        if ((~(sid + length + err + sum(params))) & 0xFF) != chk:
            self.stats["bad_checksum"] += 1
            raise BusError(f"{who} bad checksum (ff ff {hexs(rest + body)})")
        if expect_id is not None and sid != expect_id:
            self.stats["bad_id"] += 1
            raise BusError(f"Expected reply from id {expect_id}, got id {sid} (ff ff {hexs(rest + body)})")
        self.stats["rx_ok"] += 1
        if self.trace and self._last[1] not in self.trace_skip:
            self.trace(f"← #{sid} err=0x{err:02X} {hexs(b'\xff\xff' + rest + body)}"
                       + (f"  data {list(params)}" if params else ""))
        return sid, err, bytes(params)

    # ---- Commands ----
    def ping(self, sid):
        with self._io:
            self._send(sid, PING)
            try:
                _, err, _ = self._read_status(sid)
                return err
            except BusError:
                return None

    def read(self, sid, addr, n):
        with self._io:
            self._send(sid, READ, bytes([addr, n]))
            try:
                _, err, params = self._read_status(sid)
            except BusError:
                self._note(sid, False)
                raise
            self._note(sid, True, err)
            if len(params) != n:
                raise BusError(f"id {sid} read addr {addr} len {n}: got {len(params)} bytes")
            return err, params

    def write(self, sid, addr, data, wait=True):
        with self._io:
            self._send(sid, WRITE, bytes([addr]) + bytes(data))
            if not wait or sid == BROADCAST:
                return None
            try:
                _, err, _ = self._read_status(sid)
                return err
            except BusTimeout:
                return None

    def write_u8(self, sid, addr, v):
        return self.write(sid, addr, [v & 0xFF])

    def write_u16(self, sid, addr, v):
        return self.write(sid, addr, struct.pack("<H", v & 0xFFFF))

    def read_u8(self, sid, addr):
        return self.read(sid, addr, 1)[1][0]

    def read_u16(self, sid, addr):
        return struct.unpack("<H", self.read(sid, addr, 2)[1])[0]

    def sync_write(self, addr, length, id_values):
        """id_values: [(id, bytes), ...], each bytes of length `length`."""
        params = bytes([addr, length])
        for sid, data in id_values:
            assert len(data) == length
            params += bytes([sid]) + bytes(data)
        with self._io:
            self._send(BROADCAST, SYNC_WRITE, params)

    def sync_read(self, ids, addr, n):
        """Return {id: (err, params) or None} indexed by responding servo ID."""
        out = dict.fromkeys(ids)
        with self._io:
            self._send(BROADCAST, SYNC_READ, bytes([addr, n]) + bytes(ids))
            for _ in ids:
                try:
                    sid, err, params = self._read_status()
                except BusTimeout:
                    break
                except BusError:
                    continue
                if sid not in out:
                    self.stats["bad_id"] += 1
                elif len(params) == n and out[sid] is None:
                    out[sid] = (err, params)
            for sid, v in out.items():
                self._note(sid, v is not None, v[0] if v else 0)
        return out

    def reboot(self, sid):
        """Send 0x08 REBOOT (no reply; returns after ~800 ms)."""
        with self._io:
            self._send(sid, REBOOT)

    def calibrate_to(self, sid, value=None):
        """Send 0x0B position calibration to set current position to `value` (default 2048)."""
        params = b"" if value is None else struct.pack("<H", value & 0xFFFF)
        with self._io:
            self._send(sid, CALIBRATE, params)
            try:
                return self._read_status(sid)[1]
            except BusTimeout:
                return None

    def unlock(self, sid):
        return self.write_u8(sid, 55, 0)

    def lock(self, sid):
        return self.write_u8(sid, 55, 1)

    # ---- High-level helpers ----
    def state(self, sid):
        """Read 15 bytes from 56..70 (pos, speed, load, volt, temp, async, status, moving, goal, current)."""
        err, p = self.read(sid, 56, 15)
        return self._decode_state(err, p)

    @staticmethod
    def _decode_state(err, p):
        pos, spd, load = struct.unpack_from("<HHH", p, 0)
        goal, cur = struct.unpack_from("<HH", p, 11)
        return {
            "err": err, "pos": sign15(pos), "speed": sign15(spd), "load": sign10(load),
            "volt": p[6] / 10.0, "temp": p[7], "status": p[9], "moving": p[10],
            "goal": sign15(goal), "current_ma": sign15(cur) * 6.5,
            "status_text": ",".join(n for b, n in STATUS_BITS.items() if p[9] >> b & 1) or "",
        }

    def states(self, ids):
        """Sync-read state for all `ids`; failed IDs map to None."""
        raw = self.sync_read(ids, 56, 15)
        return {sid: (self._decode_state(*v) if v else None) for sid, v in raw.items()}

    def dump(self, sid):
        """Read registers 0..86 and decode using REGISTERS table."""
        raw = b""
        for start in range(0, DUMP_END, 10):
            n = min(10, DUMP_END - start)
            for attempt in range(4):
                try:
                    raw += self.read(sid, start, n)[1]
                    break
                except BusError as e:
                    if attempt == 3:
                        raise BusError(f"Dump failed at addr {start}: {e}")
                    time.sleep(0.01)
        rows = []
        for addr in sorted(REGISTERS):
            name, size, area = REGISTERS[addr]
            v = raw[addr] if size == 1 else struct.unpack_from("<H", raw, addr)[0]
            rows.append({"addr": addr, "name": name, "area": area, "size": size, "value": v})
        return {"rows": rows, "raw": list(raw)}

    def set_id(self, old, new):
        self.unlock(old)
        self.write(old, 5, [new & 0xFF], wait=False)
        time.sleep(0.02)
        self.lock(new)
        return self.ping(new) is not None
