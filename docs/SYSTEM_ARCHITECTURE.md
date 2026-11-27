# Armature Duck 2.0 — System Architecture

Comprehensive architectural documentation of the Armature Duck 2.0 (Indie Duck) hardware, embedded daemons, real-time control loops, and Microduck Studio web stack.

---

## 1. System Block Diagram

```
+─────────────────────────────────────────────────────────────────────────────+
|                         APPLICATION & USER INTERFACE                        |
|                                                                             |
|   Desktop Browser (#111110 Dark UI)            Mobile Web App (/m)          |
|   - 3D Engineering Model (Three.js)            - Touch WebPad Joystick      |
|   - Real-time Servo & IMU Calibration          - Mobile Microphone (HTTPS)  |
|   - 8x8 Depth Frustum (tof3d.js)               - Offline Vosk Voice Trigger |
|   - Actions & Poses Drawer                     - Maintenance & Safe Shutdown|
+──────────────────────────────────────┬──────────────────────────────────────+
                                       │ WebSocket (/ws, 10 Hz) & HTTP REST
+──────────────────────────────────────▼──────────────────────────────────────+
|                 RADXA ZERO 3W (Debian Bookworm, Python 3.13)                |
|                                                                             |
|  [Microduck Studio Service] (Port 8000)                                     |
|   - FastAPI / Starlette / Uvicorn backend                                   |
|   - Local CA & HTTPS certificate bridge (phone_tls.py)                      |
|   - Dual-listen audio router (dual_listen.py)                               |
|   - System health, thermal diagnostics & maintenance worker                 |
|                                                                             |
|  [Native Daemons & IPC Sockets]                                             |
|   - robotd (/run/robotd.sock, Protocol v40): 50 Hz RL neural gait loop     |
|   - tofd (/run/tofd/tof.sock): 8x8 VL53L5CX multizone depth service        |
|   - mediad (WebRTC :8443): Sony IMX219 camera H.264 video pipeline          |
|   - duck-echo (/run/duck-echo.sock): Offline Vosk speech recognition engine |
|   - microduck-helper: Privileged root helper for bus switching & calibration|
+──────────────────────────────────────┬──────────────────────────────────────+
                                       │ Hardware UART2 (/dev/ttyS2, 1 Mbps)
                                       │ I2C3 (Qwiic), I2S Audio, MIPI CSI-2
+──────────────────────────────────────▼──────────────────────────────────────+
|              POLLEN ROBOTICS 4-LAYER RPI ROBOT HAT CARRIER BOARD            |
|                                                                             |
|  - Power: Diodes Inc. AP63205 buck regulator (2S LiPo 7.4V -> 5V 2A SBC)    |
|  - Bus Transceiver: 74LVC single-wire half-duplex TTL converter             |
|  - Audio: TI TLV320AIC3104 codec + PAM8406 Class-D amp (Speaker + MEMS mic)|
|  - Connectors: J13/J14 servo ports (JST-EH 3P), Qwiic I2C ports (JST-SH 4P)|
+───────────────────┬─────────────────────────────────────┬───────────────────+
                    │                                     │
                    ▼                                     ▼
     +─────────────────────────────+       +─────────────────────────────+
     |   15x FEETECH HD-1910 BUS   |       |      SENSORS & PAYLOAD      |
     | - Left Leg: IDs 1, 2, 3, 4, 5|       | - IMU_TO_DXL V1.0A:         |
     | - Right Leg: IDs 6, 7, 8,9,10|       |   STM32G031 + LSM6DSV16X    |
     | - Head/Neck: IDs 11..15     |       |   (Feetech slave ID 200)    |
     | - 1,000,000 baud TTL        |       | - SparkFun VL53L5CX ToF     |
     | - 2048 center position      |       | - Sony IMX219 8MP CSI Cam   |
     +─────────────────────────────+       +─────────────────────────────+
```

---

## 2. Hardware Subsystems

### 2.1 Single Board Computer: Radxa Zero 3W
- **SoC**: Rockchip RK3566 (Quad-core ARM Cortex-A55 @ 1.8 GHz, Mali-G52 GPU, 0.8 TOPS NPU).
- **RAM**: 2 GB LPDDR4.
- **Wireless**: Wi-Fi 5 (802.11ac) & Bluetooth 5.0 BLE.
- **Interfaces**: 40-pin GPIO, 22-pin 0.5mm MIPI CSI-2, USB 2.0 Type-C (Host), USB 3.0 Type-C (Power/OTG).

### 2.2 Carrier Board: Pollen Robotics 4-Layer RPI Robot HAT
- **Power Management**:
  - Direct 2S battery input (6.0 V – 8.4 V).
  - High-efficiency synchronous buck regulator (Diodes Inc. AP63205) generating 5.0 V @ 2A to power the Radxa SBC directly over GPIO pins 2 & 4.
  - Dedicated 7A copper power trace supplying unregulated battery voltage to servo headers J13 and J14.
- **Half-Duplex Bus Driver**:
  - Texas Instruments 74LVC single-wire transceiver circuit connecting Radxa hardware UART2 (`/dev/ttyS2`) TX/RX lines into a single bidirectional TTL data wire.
- **Audio Processing**:
  - Texas Instruments TLV320AIC3104 I2S stereo audio codec.
  - PAM8406 3W Class-D audio amplifier driving the micro speaker.
  - Onboard Knowles MEMS microphone.

### 2.3 Actuators: Feetech HD-1910 Smart Digital Servos
- **Protocol**: Feetech STS / SCS half-duplex serial protocol.
- **Baud Rate**: 1,000,000 bps (1 Mbps).
- **Resolution**: 12-bit magnetic encoder (4096 counts per revolution; 2048 center).
- **Topology**: Daisy-chained over 3 branches through `wire_hub_1V0A`.

---

## 3. Software Stack & Control Loop Architecture

### 3.1 Motion Daemon (`robotd`)
- Implemented in Rust for deterministic timing.
- Runs a **50 Hz (20 ms)** closed-loop real-time control cycle:
  1. Synchronously broadcasts position/state query to all 15 Feetech servos and reads back positions, velocities, loads, and temperatures.
  2. Queries `IMU_TO_DXL` (slave ID 200) for attitude quaternion and angular rate.
  3. Evaluates reinforcement learning (RL) neural gait policies for bipedal stepping, dynamic balance, and posture transitions.
  4. Broadcasts target servo positions and feedforward torques.
- Exposes Unix domain socket `/run/robotd.sock` using JSON-RPC Protocol v40.

### 3.2 Microduck Studio
- **Backend Architecture**:
  - Python 3.13, Starlette, Uvicorn, and AnyIO.
  - WebSocket hub (`/ws`) providing 10 Hz state push when browser tab is focused, dropping to 1 Hz when backgrounded to conserve CPU and Wi-Fi bandwidth.
  - Privileged bus switching: Studio communicates with `microduck-helper` via sudoers rules to cleanly take over `/dev/ttyS2` for servo calibration without stopping other robot services.
- **Frontend Architecture**:
  - Vanilla modern ES modules with zero bundle/build overhead.
  - Preact + HTM for reactive UI rendering.
  - Three.js 3D canvas rendering real-time robot kinematics, Center of Mass (CoM), joint thermal/torque indicators, and 8×8 ToF volumetric depth cones (`tof3d.js`).
  - Armature design tokens (`tokens.css`) with obsidian paper dark theme (`#111110`) and clean contrast light mode toggle.

### 3.3 Audio & Offline Voice Pipeline
- **Dual Audio Ingestion**:
  1. **Physical Onboard Mic**: Knowles MEMS microphone on Pi Robot HAT sampled via I2S.
  2. **Mobile Browser Mic**: HTML5 AudioContext streams 16 kHz PCM over HTTPS WebSocket (`/m`) to `duck-echo`.
- **Speech Recognition**:
  - Runs local **Vosk** engine offline with custom phoneme grammar.
  - Requires zero internet connection, working seamlessly over the robot's Wi-Fi hotspot (`10.42.0.1`).
