# Getting Started with Armature Duck 2.0 (Indie Duck)

Welcome to **Armature Duck 2.0 (Indie Duck)**! This repository contains everything needed to source, 3D-print, assemble, calibrate, and program your own autonomous bipedal robot duck.

Whether you are starting from scratch or resuming an existing build, follow this guide step-by-step.

---

## Architecture Overview

```
                      +-----------------------------------+
                      |       User Interface Layer        |
                      |  Microduck Studio (Web / Mobile)  |
                      +-----------------+-----------------+
                                        | (HTTP/WS / REST API)
                      +-----------------v-----------------+
                      |    Single Board Computer (SBC)    |
                      |  Radxa Zero 3W (Debian / Linux)   |
                      |   - Microduck Studio Service      |
                      |   - robotd (Motion Controller)    |
                      |   - Offline Vosk Voice Engine     |
                      +-----------------+-----------------+
                                        | (UART2 / I2S / I2C)
                      +-----------------v-----------------+
                      |      Official RPI Robot HAT       |
                      |   - AP63205 5V 2A Buck Regulator  |
                      |   - 74LVC Single-Wire Bus Driver  |
                      |   - TLV320AIC3104 Codec + PAM8406 |
                      +-----------------+-----------------+
                                        |
                 +----------------------+----------------------+
                 | (1 Mbps Serial TTL Bus)                     | (I2C / Bus IMU)
      +----------v----------+                       +----------v----------+
      | 15x Feetech HD-1910 |                       |   BNO08x / LSM6DS   |
      | Serial Bus Servos   |                       | 6-Axis / 9-Axis IMU |
      +---------------------+                       +---------------------+
```

---

## 1. Hardware & Bill of Materials (BOM)

All components are standard, open, and readily procurable:
- **Comprehensive Spreadsheet**: See [`BOM/Microduck_Replica_Electronics_BOM.xlsx`](BOM/Microduck_Replica_Electronics_BOM.xlsx) for exact part numbers, electrical ratings, pinouts, and supplier links.
- **Detailed Component Reference**: See [`BOM.en.md`](BOM.en.md) (or [`BOM.md`](BOM.md) for Chinese).

### Summary of Key Parts
| Category | Component | Specification | Quantity |
| :--- | :--- | :--- | :--- |
| **Compute** | Radxa Zero 3W | Quad-core Cortex-A55, 1GB/2GB/4GB RAM, WiFi 5 / BLE | 1 |
| **Carrier HAT** | RPI Robot HAT | Official Pollen Robotics 4-layer board (5V buck, TTL bus, audio codec) | 1 |
| **Actuators** | Feetech HD-1910 | High-torque serial bus digital servos (1000000 baud TTL) | 15 |
| **IMU** | BNO08x / LSM6DSV16X | Low-drift 6-axis / 9-axis IMU (bus ID 200 or I2C) | 1 |
| **Battery** | 2S LiPo / Li-ion | 7.4 V nominal (6.0 V – 8.4 V range), 1500–2200 mAh, XT30 / JST | 1 |
| **Speaker & Mic** | Micro dynamic speaker + MEMS | 8 Ohm 1W micro speaker + onboard I2S microphone | 1 ea |
| **Fasteners** | Metric hardware | M2, M2.5, and M3 socket head screws, brass heat-set inserts | 1 set |

---

## 2. 3D Printing & Mechanical Assembly

1. **Printing Files**:
   - Printable STLs and Bambu Studio 3MF projects are available in the [CAD & Print repository](https://github.com/fanhao375/microduck-replica-cad).
   - Recommended Material: **PETG** or **PLA-CF** for structural strength and thermal stability.
2. **Mechanical Assembly**:
   - Follow the step-by-step [Assembly Manual](https://github.com/fanhao375/microduck-replica-cad/blob/master/安装说明书/microduck装配安装说明书.pdf) and assembly drawings in [`assembly-drawings/`](assembly-drawings/).
   - Install brass heat-set threaded inserts using a soldering iron with a clean tip.
   - Assemble the kinematic leg linkages, ensuring smooth, unbinding rotation at every joint.

---

## 3. Flashing the OS Disk Image

The robot runs a tailored Debian-based Armbian system with all drivers, real-time control daemons, and Microduck Studio pre-configured.

1. **Obtain the Image**:
   - Download the verified image (`microduck-replica-20261104.img.xz` or latest release from GitHub Releases).
   - Verify integrity:
     ```bash
     sha256sum -c microduck-replica-20261104.img.xz.sha256
     ```
2. **Flash to MicroSD Card**:
   - Use a high-quality Class 10 / A2 MicroSD card (16 GB or larger).
   - Write using [BalenaEtcher](https://etcher.balena.io/) or [Raspberry Pi Imager].
3. **Insert & Boot**:
   - Insert the MicroSD card into the Radxa Zero 3W.
   - Power on the robot via the 2S battery or 5V USB-C debugging power.

---

## 4. Network Setup & First Connection

When booting for the first time without configured Wi-Fi:
1. **Connect to Built-in Hotspot**:
   - Look for the Wi-Fi network: **`Microduck-Hotspot`**
   - Default password: `microduck` (or open depending on image configuration).
2. **Access Microduck Studio**:
   - Open a browser on your phone or PC:
     ```
     http://10.42.0.1:8000/
     ```
3. **Configure Home Wi-Fi**:
   - Navigate to **More → Wi-Fi Settings** inside Studio.
   - Select your home Wi-Fi SSID, enter the password, and connect.
   - Once connected, you can access Studio directly from your local network at `http://<duck-ip>:8000/`.

---

## 5. Actuator & IMU Calibration

Before walking, the 15 servos and IMU must be zeroed:
1. **Bus Scan**:
   - Open the **Servos** tab in Microduck Studio.
   - Studio automatically pings the serial bus (`/dev/ttyS2`) and lists all detected servo IDs (1 through 15).
2. **Zero Position Calibration**:
   - Position the duck into its standard zero jig posture (legs straight, feet parallel).
   - Click **Calibrate All to Center (2048)** or calibrate individual joints.
3. **IMU Verification**:
   - Move the robot by hand and verify that the 3D visualizer in Studio tracks physical pitch, roll, and yaw accurately.
   - If mounting orientation differs, use the **IMU Wizard** to update the quaternion transformation matrix.

---

## 6. Operating the Robot

Once calibrated, switch the robot into motion mode:

### 1. Motion Modes & Poses
- **Sit / Stand**: Smoothly transition from seated posture to standing balance.
- **Tabletop Walk**: Closed-loop dynamic bipedal gait with IMU feedback.
- **Roll & Rise**: Forward roll maneuver followed by autonomous self-righting recovery.

### 2. Control Interfaces
- **WebPad**: Use the touch joystick in Microduck Studio from any phone or tablet browser.
- **Bluetooth Gamepad**: Pair standard Bluetooth controllers (e.g. Xbox, DualShock) via Studio or `robotctl pad pair`.
- **Offline Voice Control**: Connect via HTTPS (`https://<duck-ip>:8000/m`) to stream microphone audio directly to the onboard Vosk speech recognizer.
- **Hardware E-Stop**: Instantly disengage servo torque from Studio or physical button.

---

## 7. Developer & Customization Guide

### Software Stack
- **Microduck Studio**: Located in [`software/studio/`](software/studio/).
  - Backend: Python 3, FastAPI, Uvicorn, Starlette.
  - Frontend: Vanilla modern ES modules, CSS custom properties, zero external build dependencies.
  - Isolated dependencies pinned in `deps/`.
- **Motion Daemon (`robotd`)**: Real-time 50 Hz control loop reading `/dev/ttyS2` and executing RL neural gait models.

### Running Local Studio Tests
```bash
cd software/studio
python3 -I run_tests.py
```

---

## Need Help?
- Browse technical guides in [`docs/`](docs/).
- Open an Issue or Discussion on the [Armature Duck 2.0 GitHub repository](https://github.com/armature-ai-labs/Armature-Duck-2.0).

---

## Author & Lead Maintainer

- **Ronie Joseph** ([@roniejosephv-star](https://github.com/roniejosephv-star))  
  *Lead Systems Developer & Creator* · [roniejosephv@gmail.com](mailto:roniejosephv@gmail.com)
- Published under **[Armature AI Labs](https://github.com/armature-ai-labs)** ([armature-ai-labs](https://github.com/armature-ai-labs))

