# Armature Duck 2.0 — Resumption Guide

This guide enables you or any collaborator to immediately resume the Armature Duck 2.0 (Indie Duck) build on a new computer (macOS, Windows, or Linux) with zero lost context.

---

## 1. Quickstart on Your New Machine

### 1.1 Clone the Repository
```bash
git clone https://github.com/armature-ai-labs/Armature-Duck-2.0.git
cd Armature-Duck-2.0
```

### 1.2 Python Environment Setup
We recommend Python 3.11 or newer (Python 3.13 is used on the duck):
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r software/studio/deps/requirements-dev.txt 2>/dev/null || true
```

---

## 2. Current Project State & Inventory

All essential hardware has been sourced, assembled, and verified:

| Component | Status | Specifications & Notes |
| :--- | :--- | :--- |
| **SBC** | In Hand | Radxa Zero 3W (`RS107-D2E0H1W15`, RK3566, 2GB RAM, Wi-Fi 5) |
| **Carrier HAT** | Assembled | Pollen Robotics 4-layer Pi Robot HAT (AP63205 buck, UART2 on `/dev/ttyS2`, TLV320AIC3104 codec + PAM8406 amp) |
| **Actuators** | In Hand | 15× Feetech HD-1910 serial bus servos (1000000 baud TTL) |
| **Custom PCB 1** | Assembled | `IMU_TO_DXL V1.0A`: STM32G031 MCU + LSM6DSV16X 6-axis IMU (emulates Feetech slave ID 200 on bus) |
| **Custom PCB 2** | Assembled | `wire_hub_1V0A`: 4-way power & bus distribution board |
| **Custom PCB 3** | Assembled | `POGO_PIN_1V0A`: Battery dock interface board |
| **Battery** | In Hand | BAK NMC 18650 2S1P 3350 mAh 10A Li-ion pack (7.4 V nominal, XT30 connector) |
| **Sensors** | In Hand | Sony IMX219 8MP CSI camera & SparkFun Qwiic VL53L5CX 8×8 ToF |
| **Software** | Deployed | Armature Studio deployed and verified on the robot at `duck-c4ef.local` |

---

## 3. Robot Network & Access Details

When powered on, the duck connects to your local network or hosts its own hotspot:

- **Hostname**: `duck-c4ef.local`
- **Known Lab IP**: `192.168.100.245`
- **Fallback Hotspot**: `Microduck-Hotspot` (IP: `10.42.0.1`, default password: `microduck`)
- **SSH Access**:
  ```bash
  ssh radxa@duck-c4ef.local
  ```
  *(Default password: `radxa`)*
- **Microduck Studio Web Console**:
  - HTTP: `http://duck-c4ef.local:8000/` (or `http://192.168.100.245:8000/`)
  - HTTPS (for mobile mic audio): `https://duck-c4ef.local:8000/m`
- **Service Paths on the Duck**:
  - Live Studio installation: `/opt/microduck/software/current/studio`
  - Dev staging area: `/home/radxa/studio-dev`
  - Systemd service: `microduck-studio.service`
  - Service restart: `sudo systemctl restart microduck-studio`

---

## 4. Immediate Resumption Action Checklist

Follow this checklist to complete the remaining build and commissioning steps:

### [ ] Step 1: Mechanical Assembly & Horn Alignment
- 3D-print chassis parts in PETG or PLA-CF using Bambu Studio 3MF files from the [CAD Repository](https://github.com/fanhao375/microduck-replica-cad).
- Install brass heat-set inserts using a clean soldering iron tip.
- Mount the 15 Feetech servos with output shafts centered (raw tick 2048) before attaching joint linkages.
- Assemble leg linkages, checking for free, unbinding rotation.

### [ ] Step 2: Wiring Harness Installation
- Follow [`docs/HARDWARE_WIRING_GUIDE.md`](docs/HARDWARE_WIRING_GUIDE.md) to route the 17 cables (`CBL-01` to `CBL-17`).
- Connect `POGO_PIN_1V0A` $\rightarrow$ Power Switch $\rightarrow$ 5A Fuse $\rightarrow$ XT30U $\rightarrow$ Pi Robot HAT power terminals.
- Connect Pi Robot HAT J13/J14 headers to `wire_hub_1V0A`.
- Connect `wire_hub_1V0A` to Left Leg chain, Right Leg chain, Head chain, and `IMU_TO_DXL V1.0A`.
- Connect VL53L5CX ToF to Qwiic port (J5–J8) and IMX219 camera to Radxa CSI.

### [ ] Step 3: Servo Bus Scan & Calibration
1. Open Microduck Studio at `http://duck-c4ef.local:8000/`.
2. Navigate to the **Servos** tab.
3. Click **Scan Bus**: confirm all 15 servos (IDs 1 through 15) reply without timeout or checksum errors.
4. Put the robot in the standard calibration jig posture (legs straight down, feet parallel).
5. Click **Calibrate All (2048)** and commit calibration to `hd1910-calibration.toml`.

### [ ] Step 4: Bus IMU Verification
1. In Microduck Studio, go to the **IMU** tab.
2. Confirm the bus IMU is responding as slave ID 200.
3. Tilt the robot chassis by hand; verify that the 3D model in Studio tracks physical roll and pitch in real time.
4. If the orientation is inverted or tilted, run the **IMU Calibration Wizard** to commit the mounting quaternion.

### [ ] Step 5: Live Gait Acceptance
1. In Studio, switch from Commissioning Mode to **Motion Mode**.
2. Click **Stand**: verify smooth transition from seated rest to upright balance.
3. Click **Tabletop Walk**: verify dynamic closed-loop bipedal stepping.
4. Test emergency stop (E-Stop button in Studio or spacebar) to confirm instant torque release.

---

## 5. Deploying Code Changes to the Duck

Whenever you edit Studio backend or frontend files on your local computer, use our deployment tool to push changes to the robot:

```bash
# Deploy changes and restart Studio service on duck
python3 tools/deploy_to_duck.py

# Or specify a different IP / hostname if needed:
python3 tools/deploy_to_duck.py --host 192.168.100.245
```

For full details on the wiring harness and architecture:
- 📖 [Hardware Wiring Guide](docs/HARDWARE_WIRING_GUIDE.md)
- 📐 [System Architecture Documentation](docs/SYSTEM_ARCHITECTURE.md)
- 📊 [Full Electronics BOM Spreadsheet](BOM/Microduck_Replica_Electronics_BOM.xlsx)
