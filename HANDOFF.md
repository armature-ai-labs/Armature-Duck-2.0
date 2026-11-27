# Armature Duck 2.0 (Indie Duck) — AI Agent & Developer Handoff Document

> **FOR ANY CLOUD LLM / AI AGENT (Claude, ChatGPT, Gemini, Antigravity, Cursor, etc.):**  
> Read this document first! It provides the complete context, architectural ground truth, hardware inventory, network coordinates, and immediate next tasks for the **Armature Duck 2.0** project. You can immediately continue development and testing without asking introductory questions.

---

## 1. Project Overview & Attribution

- **Project Name**: Armature Duck 2.0 (Indie Duck)
- **GitHub Repository**: [`https://github.com/armature-ai-labs/Armature-Duck-2.0`](https://github.com/armature-ai-labs/Armature-Duck-2.0)
- **Author & Creator**: **Ronie Joseph** ([@roniejosephv-star](https://github.com/roniejosephv-star))  
  Email: `roniejosephv@gmail.com`
- **Publishing Organization**: **[Armature AI Labs](https://github.com/armature-ai-labs)** (`armature-ai-labs`)  
  Website: [https://armatureailabs.com](https://armatureailabs.com)
- **Upstream Origin**: Open-source [Microduck](https://pollen-robotics.com/microduck/) replica project originally initiated by [fanhao375](https://github.com/fanhao375/microduck-replica).

---

## 2. Hardware Architecture & Inventory Ground Truth

The hardware components are purchased, custom PCBs assembled, and verified in hand:

```
                                  [2S Li-ion Battery 7.4V (BAK 3350mAh)]
                                                    │
                                      (CBL-01 to CBL-04, 18 AWG)
                                                    ▼
                                  [KCD1-11 Switch + 5A Blade Fuse]
                                                    │
                                         (Amass XT30U Disconnect)
                                                    ▼
                                  [Pi Robot HAT Main Power Terminal]
                                                    │
                    ┌───────────────────────────────┴───────────────────────────────┐
                    ▼                                                               ▼
        [AP63205 5V 2A Buck Regulator]                                  [Direct 7.4V Bus Feed]
                    │                                                               │
          (40-Pin GPIO Stacking Header)                                     (JST-EH 3P, CBL-07)
                    ▼                                                               ▼
           [Radxa Zero 3W SBC]                                              [wire_hub_1V0A]
      (Pins 2 & 4: 5V power, Pin 6: GND)                                            │
      (UART2 /dev/ttyS2: Pins 8 & 10)                               ┌───────────────┼───────────────┐
      (I2C3: Pins 3 & 5, I2S Audio Codec)                           ▼               ▼               ▼
                    │                                          [Left Leg]      [Right Leg]     [Head/Neck]
       ┌────────────┴────────────┐                             (5x Servos)     (5x Servos)     (5x Servos)
       ▼                         ▼                              (IDs 1..5)      (IDs 6..10)    (IDs 11..15)
[Sony IMX219 Cam]      [SparkFun ToF VL53L5CX]                                      │
(22-Pin FPC CSI)       (Qwiic J5..J8 JST-SH 4P)                             (CBL-08, 1.2mm ACH)
                                                                                    ▼
                                                                           [IMU_TO_DXL V1.0A]
                                                                         (Slave ID 200 Bus IMU)
```

### Component Details
1. **Compute**: Radxa Zero 3W (`RS107-D2E0H1W15`, Rockchip RK3566, 2GB LPDDR4 RAM, Wi-Fi 5 / BLE).
2. **Carrier HAT**: Pollen Robotics Official 4-Layer Pi Robot HAT (`pollen-robotics/elec_RPI_Robot_HAT`).
   - Integrated AP63205 buck: powers Radxa directly through GPIO Pins 2 & 4 (No external UBEC needed).
   - Integrated 74LVC single-wire TTL half-duplex transceiver: bridges Radxa UART2 (`/dev/ttyS2`) to servo ports J13 and J14.
   - Integrated TI TLV320AIC3104 audio codec + PAM8406 amplifier: drives 4Ω 3W micro speaker and onboard Knowles MEMS microphone over I2S.
   - Dedicated Qwiic ports (J5–J8, JST-SH 4-pin) on hardware I2C3.
3. **Actuators**: 15× Feetech HD-1910 smart digital bus servos (1000000 baud TTL, 2048 center tick).
4. **Custom Assembled PCBs**:
   - `IMU_TO_DXL V1.0A`: Bus IMU combining STM32G031 MCU + LSM6DSV16X 6-axis IMU, emulating Feetech slave ID 200 on the bus.
   - `wire_hub_1V0A`: 4-way power and serial bus splitter board.
   - `POGO_PIN_1V0A`: Battery dock/pogo interface.
5. **Sensors**: Sony IMX219 8MP CSI camera module, SparkFun Qwiic VL53L5CX 8×8 multizone ToF.
6. **Battery**: BAK NMC 18650 2S1P 3350 mAh 10A Li-ion pack (7.4 V nominal, XT30 connector).
7. **Complete 17-Cable Matrix**: Fully documented in [`docs/HARDWARE_WIRING_GUIDE.md`](docs/HARDWARE_WIRING_GUIDE.md) and [`BOM/Microduck_Replica_Electronics_BOM.xlsx`](BOM/Microduck_Replica_Electronics_BOM.xlsx).

---

## 3. Robot Network & Connection Ground Truth

- **Duck Hostname**: `duck-c4ef.local`
- **Known Lab Static IP**: `192.168.100.245`
- **Fallback Hotspot**: SSID `Microduck-Hotspot` (Duck IP: `10.42.0.1`, default password: `microduck`)
- **SSH Credentials**:
  ```bash
  ssh radxa@duck-c4ef.local
  # Password: radxa
  ```
- **Microduck Studio Web UI**:
  - HTTP: `http://duck-c4ef.local:8000/` (or `http://192.168.100.245:8000/`)
  - HTTPS (Mobile mic audio): `https://duck-c4ef.local:8000/m`
- **Service Paths on Robot**:
  - Live Production Studio: `/opt/microduck/software/current/studio`
  - Dev Staging Directory: `/home/radxa/studio-dev`
  - Systemd Service: `microduck-studio.service`
  - Fast Cutover Script on Robot: `sudo /home/radxa/deploy_armature_studio.sh`

---

## 4. Software Stack & What Was Accomplished

1. **100% English Localization & CJK Removal**:
   - Every file across `software/studio` (Python backend and Web UI) has been translated to clean English.
   - Enforced by automated gate test: `python3 software/studio/tests/test_no_cjk.py` (passes 100%).
2. **Armature AI Labs Visual Redesign**:
   - Dark obsidian paper theme (`#111110`, `#171716`) with warm cream typography and amber accents in `web/tokens.css`.
   - SVG 8-segment commutator animated mark: `web/armature-mark.js`.
   - Three.js 8×8 volumetric depth frustum visualizer: `web/tof3d.js`.
   - Vendored offline typography in `web/vendor/fonts/` (Instrument Serif, Space Grotesk, Space Mono) for standalone AP operation.
3. **New Production Modules Added**:
   - `web/actions.js` & `web/actions.css`: Action drawer (Roll & rise, sit/stand, tabletop walk, kicks).
   - `web/communication-health.js` & `.css`: Real-time serial bus packet loss, latency, and error counter.
   - `web/maintenance.js` & `deploy/microduck-maintenance`: Safe maintenance mode, config backup/restore, safe shutdown.
   - `web/runtime-diagnostics.js`: Live CPU, thermals, voltage, and system metrics.
   - `deploy/phone_tls.py` & `web/certificate-download.js`: Local CA and self-signed HTTPS certificate generator allowing mobile browsers to stream microphone audio to `duck-echo`.
   - `studio/dual_listen.py` & `studio/microphone_settings.py`: Dual-microphone audio routing.
4. **Cross-Platform Deployment Tool**:
   - `tools/deploy_to_duck.py` (and `tools/deploy_to_duck.sh`): Allows syncing local studio changes to `duck-c4ef.local` over SSH/rsync from Windows, macOS, or Linux, executing selfcheck, and restarting the service.

---

## 5. Repository Directory Map

```
Armature-Duck-2.0/
├── HANDOFF.md                                    # THIS FILE (Comprehensive AI & dev handoff)
├── RESUME_GUIDE.md                               # Step-by-step resume checklist
├── GETTING_STARTED.md                            # End-to-end builder quickstart
├── AUTHORS.md                                    # Ronie Joseph & Armature AI Labs attribution
├── README.md                                     # English repository overview
├── README.zh-CN.md                               # Alternate Chinese translation
├── BOM.md                                        # English Bill of Materials
├── BOM/
│   └── Microduck_Replica_Electronics_BOM.xlsx    # Full 4-sheet Excel engineering BOM
├── docs/
│   ├── HARDWARE_WIRING_GUIDE.md                  # Complete 17-cable wiring matrix & pinouts
│   └── SYSTEM_ARCHITECTURE.md                    # Deep hardware & software technical spec
├── tools/
│   ├── deploy_to_duck.py                         # Cross-platform Python sync tool (Win/Mac/Linux)
│   └── deploy_to_duck.sh                         # Bash sync tool for Unix
└── software/
    └── studio/                                   # Microduck Studio web console & backend
        ├── run.py                                # Studio entrypoint (python3 -I run.py)
        ├── run_tests.py                          # Test suite runner
        ├── studio/                               # Python backend (FastAPI/Starlette/Uvicorn)
        ├── web/                                  # Preact + HTM vanilla JS frontend
        ├── deploy/                               # Deployment configs, systemd units, helpers
        └── tests/                                # Test suites (including test_no_cjk.py)
```

---

## 6. Immediate Next Tasks (Where to Resume)

When continuing work on this repository, focus on these physical and software acceptance tasks in sequence:

### [ ] Task 1: Servo Bus ID Scan & Horn Alignment
- **Objective**: Verify that all 15 Feetech servos communicate over `/dev/ttyS2` (1,000,000 baud).
- **Procedure**:
  1. Boot the duck and open `http://duck-c4ef.local:8000/`.
  2. Navigate to **Servos** tab and click **Scan Bus**.
  3. Ensure IDs 1 through 15 are found with 0 packet drops (check **Communication Health** tab).
  4. Ensure servo horns are mounted at mechanical zero corresponding to raw tick `2048`.

### [ ] Task 2: Bus IMU (`IMU_TO_DXL V1.0A`) Verification
- **Objective**: Verify the custom STM32G031 + LSM6DSV16X board is responding as slave ID 200 on the Feetech bus.
- **Procedure**:
  1. In Studio, check the **IMU** tab.
  2. Confirm attitude quaternion updates when the chassis is tilted.
  3. If orientation needs alignment, run the **IMU Calibration Wizard** to commit `hd1910-calibration.toml`.

### [ ] Task 3: Zero-Point Calibration Commitment
- **Objective**: Commit factory zero positions for the full kinematic tree.
- **Procedure**:
  1. Place the robot in the zero-calibration jig (legs vertical, feet square).
  2. In Studio, click **Calibrate All to Center (2048)**.
  3. Commit the calibration file via `calib-commit`.

### [ ] Task 4: Camera & Vision Pipeline Acceptance
- **Objective**: Verify Sony IMX219 MIPI CSI video feed.
- **Procedure**:
  1. In Studio, open the **Camera** tab.
  2. Verify WebRTC stream from `mediad` (:8443) renders live video.

### [ ] Task 5: Dynamic Motion & Gait Acceptance
- **Objective**: Transition to Motion Mode and execute dynamic gaits.
- **Procedure**:
  1. In Studio, switch to **Motion Mode**.
  2. Test **Sit / Stand**: verify smooth posture interpolation without servo oscillation or stall current.
  3. Test **Tabletop Walk**: verify 50 Hz closed-loop RL walking gait with active balance.
  4. Test **E-Stop**: verify spacebar or UI button immediately de-energizes servo torque.

---

## 7. Instructions for Any LLM Resuming Work

1. **Commit Attribution**: Always ensure commits use:
   - `Author: Ronie Joseph <roniejosephv@gmail.com>`
   - `Committer: Ronie Joseph <roniejosephv@gmail.com>`
2. **Language Invariant**: Do NOT introduce Chinese characters into `software/studio/`. Always run `python3 software/studio/tests/test_no_cjk.py` before committing.
3. **Deployment Workflow**:
   - Edit files locally in `software/studio/`.
   - Test locally: `python3 software/studio/tests/test_no_cjk.py`.
   - Deploy to robot: `python3 tools/deploy_to_duck.py`.
4. **No Destructive Overwrites**: Protect `BOM/Microduck_Replica_Electronics_BOM.xlsx` and `docs/HARDWARE_WIRING_GUIDE.md` as hardware source of truth.
