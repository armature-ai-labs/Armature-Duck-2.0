# Armature Duck 2.0 — Complete Hardware Wiring & Harness Guide

This guide details the complete 17-cable wiring harness (`CBL-01` through `CBL-17`), connector pinouts, wire gauges, and interconnect specifications for the Armature Duck 2.0.

---

## 1. System Power & Interconnect Architecture

```
                                  [2S Li-ion Battery 7.4V]
                                             │
                                   (CBL-01, 18 AWG Red)
                                             ▼
                                  [KCD1-11 Rocker Switch]
                                             │
                                   (CBL-02, 18 AWG Red)
                                             ▼
                                     [5A Blade Fuse]
                                             │
                                   (CBL-03, 18 AWG Red)
                                             ▼
               (CBL-04, 18 AWG Blk)    [XT30U Male]
       [Battery Ground] ─────────────────────┘
                                             │
                                    (Master Power Disconnect)
                                             ▼
                                       [XT30U Female]
                                             │
                                   (CBL-05, 18 AWG Red/Blk)
                                             ▼
                              [Pi Robot HAT Power Terminals]
                                             │
                     ┌───────────────────────┴───────────────────────┐
                     ▼                                               ▼
         [AP63205 5V 2A Buck]                           [Direct 7.4V Bus Feed]
                     │                                               │
           (CBL-06, 40-Pin Header)                         (CBL-07, JST-EH 3P)
                     ▼                                               ▼
             [Radxa Zero 3W]                                 [wire_hub_1V0A]
       (Pins 2 & 4: 5V, Pin 6: GND)                                  │
       (UART2 /dev/ttyS2: Pins 8 & 10)                 ┌─────────────┼─────────────┐
       (I2C3: Pins 3 & 5, I2S Audio)                   ▼             ▼             ▼
                                                  [Left Leg]    [Right Leg]   [Head/Neck]
                                                  (5x Servos)   (5x Servos)   (5x Servos)
                                                       │
                                              (CBL-08, 1.2mm ACH)
                                                       ▼
                                              [IMU_TO_DXL V1.0A]
                                            (Slave ID 200 Bus IMU)
```

---

## 2. Complete 17-Cable Harness Matrix

| Cable ID | Origin / From | Destination / To | Wire Spec | Connector / Termination | Description & Function |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **CBL-01** | `POGO_PIN_1V0A` BAT+ pad | KCD1-11 Rocker Switch (Pin 1) | 18 AWG Silicone Red | Direct solder pad to 4.8mm spade / soldered | Raw battery positive feed from charging/dock pogo pins |
| **CBL-02** | KCD1-11 Rocker Switch (Pin 2) | Inline Mini Blade Fuse Holder | 18 AWG Silicone Red | Soldered / heat-shrink insulated | Switched positive battery bus |
| **CBL-03** | Inline Mini Blade Fuse Holder | Amass XT30U Male (Pin 1, +) | 18 AWG Silicone Red | Soldered with adhesive heat shrink | Fused positive line (5A rated blade fuse) |
| **CBL-04** | `POGO_PIN_1V0A` GND pad | Amass XT30U Male (Pin 2, -) | 18 AWG Silicone Black | Direct solder pad to soldered pin | Main battery power ground return |
| **CBL-05** | Amass XT30U Female | Pi Robot HAT Main Power Terminal | 18 AWG Silicone Red/Black | Soldered XT30U to bare tinned wire ends | Main power disconnect harness to HAT screw block |
| **CBL-06** | Pi Robot HAT 40-Pin Female | Radxa Zero 3W 40-Pin Male Header | Direct Board Stacking | 2×20 2.54mm stacking female header | Passes 5V/2A buck power, UART2 (`/dev/ttyS2`), I2C3, I2S |
| **CBL-07** | Pi Robot HAT Servo Port (J13/J14)| `wire_hub_1V0A` Bus Input (J1) | 22 AWG (PWR) + 26 AWG (SIG)| JST-EH 3-Pin female to JST-EH 3-Pin female | Feeds 7.4V servo power and 1Mbps TTL half-duplex bus |
| **CBL-08** | `wire_hub_1V0A` Hub Port (J2) | `IMU_TO_DXL V1.0A` Bus In (J2) | 28 AWG 3-Conductor | JST-ACH 1.2mm 3-Pin to JST-ACH 1.2mm 3-Pin | Carries 7.4V, GND, and TTL bus to the STM32 bus IMU |
| **CBL-09** | `wire_hub_1V0A` Hub Port (J3) | Left Leg Servo Chain (Servo #1) | 22 AWG (PWR) + 26 AWG (SIG)| JST-EH 3-Pin to Feetech 3-Pin Molex | Feeds Left Hip Pitch, Roll, Yaw, Knee, Ankle (5 servos) |
| **CBL-10** | `wire_hub_1V0A` Hub Port (J4) | Right Leg Servo Chain (Servo #6)| 22 AWG (PWR) + 26 AWG (SIG)| JST-EH 3-Pin to Feetech 3-Pin Molex | Feeds Right Hip Pitch, Roll, Yaw, Knee, Ankle (5 servos) |
| **CBL-11** | `wire_hub_1V0A` Hub Port (J5) | Head / Neck Chain (Servo #11) | 24 AWG (PWR) + 28 AWG (SIG)| JST-EH 3-Pin to Feetech 3-Pin Molex | Feeds Neck Pitch, Head Yaw, Head Pitch, Mouth (5 servos) |
| **CBL-12** | SparkFun VL53L5CX ToF Sensor | Pi Robot HAT Qwiic Port (J5–J8) | 28 AWG 4-Conductor Ribbon | JST-SH 4-Pin (1.0mm) to JST-SH 4-Pin | 3.3V, GND, I2C3 SDA, I2C3 SCL for 8×8 depth ranging |
| **CBL-13** | Sony IMX219 8MP CSI Camera | Radxa Zero 3W CSI Camera Port | 15-Pin to 22-Pin FPC Ribbon | 15-Pin 1.0mm pitch to 22-Pin 0.5mm pitch | 2-lane MIPI CSI-2 video feed to RK3566 ISP |
| **CBL-14** | Pi Robot HAT Speaker Output | 4Ω 3W Micro Dynamic Speaker | 26 AWG Twisted Pair | Bare tinned ends to soldered speaker pads | Driven by onboard TI TLV320AIC3104 + PAM8406 amplifier |
| **CBL-15** | ST-Link V2 Mini Programmer | `IMU_TO_DXL V1.0A` SWD Port | 28 AWG 4-Conductor Dupont | 4-Pin 2.54mm female to 1.27mm micro pads | SWDIO, SWCLK, GND, 3.3V (bench firmware flashing) |
| **CBL-16** | Feetech FE-URT-2 USB Adapter | PC USB Port / Servo Daisy Chain | USB-A to USB-C + 3-Pin Molex| Standard USB to PC; 3-Pin Feetech header | Benchtop servo ID assignment & center calibration |
| **CBL-17** | B3AC Balance Charger | 2S Li-ion Battery Balance Port | 24 AWG 3-Conductor | JST-XH 3-Pin female to JST-XH 3-Pin male | 2S cell voltage balancing (3.7V / 4.2V per cell) |

---

## 3. Pinouts & Signal Specifications

### 3.1 Pi Robot HAT 40-Pin GPIO Stacking Header
| Pin # | Signal Name | Voltage | Direction | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Pin 2** | `5V_IN` | 5.0 V DC | HAT $\rightarrow$ Radxa | Main 5V 2A buck output from AP63205 regulator |
| **Pin 4** | `5V_IN` | 5.0 V DC | HAT $\rightarrow$ Radxa | Redundant 5V rail pin for low resistance |
| **Pin 6** | `GND` | 0 V | Ground | Digital & power ground reference |
| **Pin 8** | `UART2_TX` | 3.3 V TTL | Radxa $\rightarrow$ HAT | Connects to 74LVC single-wire bus driver input |
| **Pin 10** | `UART2_RX` | 3.3 V TTL | HAT $\rightarrow$ Radxa | Receive line from 74LVC single-wire half-duplex bus |
| **Pin 3** | `I2C3_SDA` | 3.3 V | Bidirectional | Hardware I2C3 data line (Qwiic sensor bus) |
| **Pin 5** | `I2C3_SCL` | 3.3 V | Radxa $\rightarrow$ HAT | Hardware I2C3 clock line (400 kHz Fast-Mode) |
| **Pins 12, 35, 38, 40** | `I2S_AUDIO` | 3.3 V | Bidirectional | BCLK, LRCK, DIN, DOUT to TLV320AIC3104 audio codec |

### 3.2 Feetech Serial Bus Connector (JST-EH 3-Pin)
| Pin # | Wire Color | Signal Name | Voltage | Description |
| :--- | :--- | :--- | :--- | :--- |
| **1** | Black | `GND` | 0 V | High-current power and signal ground return |
| **2** | Red | `V_BAT` | 6.0 V – 8.4 V | Unregulated 2S battery bus power (up to 7A peak) |
| **3** | White / Yellow | `DATA` | 3.3V / 5V TTL | Half-duplex bidirectional single-wire UART (1,000,000 baud) |

### 3.3 SparkFun Qwiic Connector (JST-SH 4-Pin)
| Pin # | Wire Color | Signal Name | Voltage | Description |
| :--- | :--- | :--- | :--- | :--- |
| **1** | Black | `GND` | 0 V | Ground reference |
| **2** | Red | `3V3` | 3.3 V DC | Regulated 3.3V power (from HAT) |
| **3** | Blue | `SDA` | 3.3 V | I2C3 Data line |
| **4** | Yellow | `SCL` | 3.3 V | I2C3 Clock line |

### 3.4 Custom Bus IMU (`IMU_TO_DXL V1.0A`) SWD Header
| Pin # | Label | Voltage | Description |
| :--- | :--- | :--- | :--- |
| **1** | `3V3` | 3.3 V DC | Target supply voltage (from ST-Link) |
| **2** | `SWDIO`| 3.3 V TTL | Serial Wire Debug Data Input/Output |
| **3** | `SWCLK`| 3.3 V TTL | Serial Wire Debug Clock line |
| **4** | `GND`  | 0 V | Common Ground |

---

## 4. Hardware Assembly Checklist

1. **Verify No Short Circuits**: Prior to connecting the battery, use a multimeter in continuity mode to verify:
   - Resistance between XT30U (+) and (-) is $> 100\ \text{k}\Omega$.
   - No short between 5V rail and GND on HAT 40-pin header.
   - No short between servo `V_BAT` and `DATA` line.
2. **First Power-Up Check**:
   - Turn on the rocker switch with no servos plugged in.
   - Verify 5.0V output on Pi Robot HAT GPIO pins 2 & 4.
   - Confirm Radxa Zero 3W power LED illuminates and boots Debian.
3. **Connect Actuator Bus**:
   - Plug in `wire_hub_1V0A` and connect servo chains.
   - In Microduck Studio, execute a bus scan to verify all 15 servos respond cleanly.
