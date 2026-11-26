# -*- coding: utf-8 -*-
"""(Ported from tools/servo-web/sim_bus.py in the same repository. Studio uses it to run the servo page on a desktop without servos connected, and tests use it too.)

Protocol-level HD-1910 simulator: pretends to be a serial port, receiving Feetech instruction packets and replying from a register table.

Unlike FakeBus in server.py (which mocks the high-level interface and does not exercise feetech.py),
this lets the real FeetechBus send and receive bytes against it, following real hardware packet framing,
checksums, sync read/write, lock, torque, and midpoint calibration preconditions.

  bus = feetech.FeetechBus(None, ser=SimSerial([20, 21, ...]))

Simulated hardware behaviors (with provenance):
- Torque switch write 128: HD-1910 (firmware 3.46) replies "success" but does nothing (verified 2026-09-20 over 4 rounds, even unlocked with torque off;
  protocol manual also states HLS >= 3.43 does not support it). When accept_128=True, follows STS rules: calibrates only when lock=0 and torque=0.
- 0x0B position calibration: sets current position reading to the parameter value (2048 if no parameter). support_0b=False simulates servos that ignore this instruction.
- Offset register 31: offset_adds controls whether position = raw + offset or raw - offset; the manual is ambiguous, so both can be simulated.
- Gravity droop: when torque turns off, joints mounted on the duck drop under gravity (measured on real neck: 80 steps in 130 ms ≈ 0.62 steps/ms).
  droop sets steps/ms drop — without this, bugs like "waiting too long after torque off and calibrating a drooped pose as zero" cannot be caught.
- Bus latency: real hardware takes ~1-2 ms per round trip (TX + reply + USB latency timer). latency_ms simulates this
  while driving a virtual clock: droop is computed on this clock so tests are deterministic regardless of machine speed; time.sleep is also accounted for.
- Register table ends at 86; over-reads return only the remaining bytes (measured on hardware: reading 10 from 80 returns 7).
- Position, speed, and current use bit 15 as the sign bit; offset register 31 also uses bit 15 for sign (observed 34279 = -1511 on hardware).
- REBOOT (0x08) sends no reply packet.
- Dual P / D / I: EEPROM 21-23 factory defaults are 32 / 40 / 0; runtime uses RAM 50-52 (copied from EEPROM on power-up/reboot),
  and torque limit 48 powers up equal to max torque 16 (hardware data: robotd diagnostic registers_before_arm eeprom_pdi / temporary_pdi).
"""
import struct
import time

from . import feetech

TABLE_END = 87


def enc15(v):
    return (0x8000 | (-v & 0x7FFF)) if v < 0 else (v & 0x7FFF)


class SimServo:
    def __init__(self, sid, phys=2048):
        self.r = bytearray(TABLE_END)
        r = self.r
        r[0], r[1], r[2], r[3], r[4] = 3, 46, 0, 3, 10      # Firmware 3.46, END=0 little-endian
        r[5], r[6], r[8] = sid, 0, 1                        # ID, baud 1M, response level 1
        struct.pack_into("<HH", r, 9, 0, 4095)
        r[13], r[15] = 70, 40
        struct.pack_into("<H", r, 16, 1000)
        r[18], r[21], r[22], r[33] = 52, 32, 40, 4          # Phase 52, factory P32 / D40 / I0 (EEPROM 21-23), mode 4
        r[50:53] = r[21:24]                                 # Runtime P / D / I (RAM 50-52): copied from EEPROM on power-up
        struct.pack_into("<H", r, 48, 1000)
        r[55] = 1                                           # Factory locked
        r[62], r[63] = 74, 30
        self.phys = phys          # Raw encoder reading (without offset)
        self.off = 0              # Offset: reported position = phys + off
        self.goal = None
        self.calibrations = 0     # Actual midpoint calibration count, for testing
        self.muted = set()        # Do not reply when reading these addresses, simulating mid-run dropout
        self.accept_128 = False   # HD-1910 ignores 128
        self.support_0b = True
        self.ignore_offset_writes = False   # Simulate servos that even ignore offset writes
        self.offset_adds = True   # Reported position = phys + off; when False, phys - off (ambiguous in manual, test both)
        self.droop = 0.0          # Steps dropped per ms when torque is off; real neck measured 0.62
        self._t = 0.0             # Virtual bus clock at last tick (ms)
        self.mute_after_calib = set()   # Add these addresses to muted right after calibration
        self.readonly = set()     # Writes to these addresses return "success" without taking effect (for readback guard tests)

    @property
    def id(self):
        return self.r[5]

    def pos(self):
        return self.phys + (self.off if self.offset_adds else -self.off)

    def tick(self, clock_ms=0.0):
        """When torque is on, move straight to goal (simulator ignores dynamics); when off, droop by droop rate.

        clock_ms is the bus virtual clock, advanced by SimSerial per packet latency and real sleep."""
        dt_ms, self._t = clock_ms - self._t, clock_ms
        if self.r[40] == 1 and self.goal is not None:
            self.phys = self.goal - (self.off if self.offset_adds else -self.off)
        elif self.droop:
            self.phys -= self.droop * dt_ms
        p = round(self.pos())
        struct.pack_into("<H", self.r, 56, enc15(p))
        struct.pack_into("<H", self.r, 67, enc15(self.goal if self.goal is not None else p))
        struct.pack_into("<H", self.r, 31, enc15(self.off))

    def write(self, addr, data):
        for k, b in enumerate(data):
            a = addr + k
            if a >= TABLE_END:
                break
            if a == 40 and b == 128:
                if self.accept_128 and self.r[40] == 0 and self.r[55] == 0:
                    self.recalibrate(2048)
                continue                                     # Reply success, do nothing
            if a in self.readonly:
                continue
            self.r[a] = b
        # Commit multi-byte registers
        if addr <= 42 < addr + len(data):
            self.goal = feetech.sign15(struct.unpack_from("<H", self.r, 42)[0])
        if addr <= 31 < addr + len(data) and not self.ignore_offset_writes:
            self.off = feetech.sign15(struct.unpack_from("<H", self.r, 31)[0])
        if addr <= 40 < addr + len(data) and self.r[40] == 1 and self.goal is None:
            self.goal = self.pos()

    def recalibrate(self, value):
        # phys can be fractional due to droop, so offset must be rounded: real servo registers hold integers only.
        self.off = round((value - self.phys) if self.offset_adds else (self.phys - value))
        self.calibrations += 1
        self.muted |= self.mute_after_calib

    def read(self, addr, n, clock_ms=0.0):
        self.tick(clock_ms)
        return bytes(self.r[addr:min(addr + n, TABLE_END)])


class SimSerial:
    """Fake serial port: write() receives instructions and generates replies from servos on the bus; read() returns them."""

    def __init__(self, ids, phys=None):
        self.timeout = 0.02
        self.servos = {i: SimServo(i, (phys or {}).get(i, 2048)) for i in ids}
        self.rx = bytearray()
        self.latency_ms = 0.0     # Round-trip latency in ms; real URT-2 hardware is ~1-2 ms
        self.clock = 0.0          # Virtual bus clock (ms): +latency per packet plus real sleep elapsed
        self._wall = time.monotonic()

    # ---- Serial interface ----
    def reset_input_buffer(self):
        self.rx.clear()

    def read(self, n=1):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def close(self):
        pass

    def write(self, data):
        data = bytes(data)
        while len(data) >= 6:
            if data[0] != 0xFF or data[1] != 0xFF:
                data = data[1:]
                continue
            sid, length = data[2], data[3]
            pkt = data[:4 + length]
            data = data[4 + length:]
            instr, params = pkt[4], pkt[5:-1]
            if ((~sum(pkt[2:-1])) & 0xFF) != pkt[-1]:
                continue                                         # Bad checksum: real servo ignores it
            self._handle(sid, instr, params)
        return len(data)

    # ---- Servos ----
    def _by_id(self, sid):
        for s in self.servos.values():
            if s.id == sid:
                return s
        return None

    def _advance(self):
        """Advance virtual clock by one bus round-trip latency plus real time slept since last call (for droop)."""
        now = time.monotonic()
        self.clock += self.latency_ms + (now - self._wall) * 1000
        self._wall = now
        return self.clock

    def _reply(self, s, params=b""):
        s.tick(self.clock)
        err = s.r[65]
        length = len(params) + 2
        body = bytes([s.id, length, err]) + bytes(params)
        self.rx += bytes([0xFF, 0xFF]) + body + bytes([(~sum(body)) & 0xFF])

    def _handle(self, sid, instr, p):
        self._advance()
        if instr == feetech.SYNC_WRITE:
            addr, ln = p[0], p[1]
            rest = p[2:]
            for k in range(0, len(rest), ln + 1):
                s = self._by_id(rest[k])
                if s:
                    s.write(addr, rest[k + 1:k + 1 + ln])
            return
        if instr == feetech.SYNC_READ:
            addr, ln = p[0], p[1]
            for i in p[2:]:
                s = self._by_id(i)
                # alive() is for the fake IMU board: simulates "bus MCU not up yet" and "board dead".
                if s and addr not in getattr(s, "muted", ()) and getattr(s, "alive", lambda _: True)(self.clock):
                    self._reply(s, s.read(addr, ln, self.clock))
            return
        s = self._by_id(sid)
        if s is None:
            return
        if instr == feetech.PING:
            if sid == feetech.BROADCAST or not getattr(s, "alive", lambda _: True)(self.clock):
                return                                       # Manual: broadcast PING is forbidden with multiple devices on bus
            self._reply(s)
        elif instr == feetech.READ:
            if p[0] in s.muted or not getattr(s, "alive", lambda _: True)(self.clock):
                return
            self._reply(s, s.read(p[0], p[1], self.clock))
        elif instr == feetech.WRITE:
            s.write(p[0], p[1:])
            self._reply(s)                                       # Replies with new ID when ID is changed
        elif instr == feetech.CALIBRATE:
            if s.support_0b:
                s.recalibrate(2048 if len(p) < 2 else feetech.sign15(p[0] | p[1] << 8))
            self._reply(s)
        elif instr == feetech.REBOOT:
            s.r[40] = 0                                          # No reply packet; returns with torque off
            s.r[50:53] = s.r[21:24]                              # RAM P/D/I and torque limit reset to power-on values
            s.r[48:50] = s.r[16:18]
