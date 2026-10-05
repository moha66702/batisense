# BatiSense Pro

**Multi-node IoT platform for real-time building monitoring: from the sensor to the dashboard.**

![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-backend-000000?logo=flask)
![STM32](https://img.shields.io/badge/STM32-Nucleo%20F446RE-03234B)
![ESP32](https://img.shields.io/badge/ESP32-CAM-E7352C)
![LoRa](https://img.shields.io/badge/LoRa-433%20MHz-00A6D6)
![MQTT](https://img.shields.io/badge/MQTT-Mosquitto-660066)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-3B%2B-C51A4A?logo=raspberrypi&logoColor=white)

BatiSense Pro is a wireless sensor network that monitors gas, motion, vibration, temperature, water and electricity consumption in a building. Sensor nodes send their data over LoRa to a Raspberry Pi gateway, which stores it and serves a live web dashboard with alerts.

It was built as an end-of-studies project (PFE) at **ENSTA Alger** (Telecommunications & Networks) and entered in the **Startup DZ** competition.

> **Live demo:** https://batisense.netlify.app (static demo with simulated sensor data, no backend)

---

## Features

- Real-time monitoring of **7 specialized nodes**: kitchen, living room, entrance door, room, water, electricity, gas.
- **Live updates** in the browser through Server-Sent Events (SSE), with no page refresh.
- **Alerts** for gas leaks, intrusion/motion, vibration and abnormal readings.
- **Automatic meter reading**: electricity (optical pulse detection) and water (camera + OCR).
- **History and charts** for every sensor (Chart.js).
- **Admin panel** with separate authentication.
- Gateway runs as a `systemd` service and starts automatically on boot.

---

## System architecture

```
 ┌──────────────────────┐
 │  STM32 kitchen node  │──┐
 │  Arduino energy node │──┤   LoRa 433 MHz     ┌──────────────────────────┐     ┌──────────────┐
 │  ESP32-CAM water node│──┼──────────────────▶ │  Raspberry Pi 3B+ gateway│───▶ │  Web dashboard│
 │  other sensor nodes  │──┘  (SF7, BW 125 kHz) │  Flask · SQLite · MQTT   │ SSE │  Chart.js     │
 └──────────────────────┘                        └──────────────────────────┘     └──────────────┘
```

---

## Hardware

| Node | Board | Sensors / modules |
|---|---|---|
| Kitchen | STM32 Nucleo F446RE | DHT11 (temperature/humidity), MQ-4 (gas), PIR HC-SR501 (motion), SW-420 (vibration), SX1278 RA-01 LoRa |
| Electricity | Arduino Uno | Photodiode / phototransistor reading the optical pulses of the electricity meter |
| Water | ESP32-CAM | Camera with AI-on-the-Edge firmware for water-meter reading |
| Gateway | Raspberry Pi 3B+ | LoRa receiver, Flask, SQLite, Mosquitto |

A custom single-sided PCB for the Arduino node was designed in **KiCad**.

### LoRa parameters

| Parameter | Value |
|---|---|
| Frequency | 433 MHz |
| Spreading factor | SF7 |
| Bandwidth | 125 kHz |
| Coding rate | 4/5 |
| Sync word | `0x34` |

---

## Software stack

- **Gateway / backend:** Python, Flask, SQLite, Mosquitto (MQTT)
- **Frontend:** HTML, CSS, JavaScript, Chart.js, Server-Sent Events
- **OCR (water meter):** EasyOCR and Tesseract
- **Firmware:** C/C++ (PlatformIO / Arduino framework)
- **Tools:** Git, KiCad, LaTeX, systemd

---

## Project structure

> Adjust this tree to match your repository.

```
BatiSense-Pro/
├── firmware/
│   ├── stm32_kitchen/       # STM32 Nucleo F446RE node
│   ├── arduino_energy/      # Arduino Uno energy node
│   └── esp32_cam_water/     # ESP32-CAM water node
├── gateway/                 # LoRa receiver on the Raspberry Pi
├── backend/                 # Flask app, API, SQLite
├── frontend/                # Dashboard (static files)
├── hardware/                # KiCad PCB and wiring diagrams
├── docs/                    # Report
├── .env.example
├── requirements.txt
└── README.md
```

---

## Getting started

### 1. Clone

```bash
git clone https://github.com/moha66702/BatiSense-Pro.git
cd BatiSense-Pro
```

### 2. Install the backend

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Copy the example file and set your own values (never commit real credentials):

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `ADMIN_USER` | Admin panel username |
| `ADMIN_PASSWORD` | Admin panel password |
| `SECRET_KEY` | Flask session secret |
| `DATA_DIR` | Folder where the database is stored |

### 4. Run

```bash
python app.py
```

Then open `http://localhost:5000`.

### 5. Gateway on the Raspberry Pi

```bash
sudo apt install mosquitto mosquitto-clients
sudo cp batisense.service /etc/systemd/system/
sudo systemctl enable --now batisense.service
```

> **Note:** On Python 3.13 / Debian Trixie, `pyLoRa` is incompatible with the default GPIO library. Install `rpi-lgpio` instead.

### 6. Flash the nodes

Open each folder in `firmware/` with PlatformIO or the Arduino IDE, then build and upload to its board.

**Wiring tips:**
- Power the LoRa module from a stable 3.3 V regulator (e.g. AMS1117-3.3).
- Never connect 5 V logic directly to the SX1278; use a level shifter.
- Both ends must use the same sync word (`0x34`).

---

## Documentation

The full engineering report (architecture, hardware design, market study, economic analysis) is in `docs/`. It was written in LaTeX (XeLaTeX, French/Arabic).

---

## Roadmap

- [ ] More node types (temperature zones, smoke, door contact)
- [ ] Mobile notifications (Telegram / push)
- [ ] OTA firmware updates
- [ ] Battery-powered nodes with deep sleep
- [ ] HTTPS and role-based access

---

## Authors

- **Djelloul Mohammed El Hacene** — [GitHub](https://github.com/moha66702) · [LinkedIn](https://linkedin.com/in/djelloul-mohammed-805b79394)
- **Kouachi Aissam**

**Supervision:** Boudiar Toufik (supervisor), Beghami Sami and Cherabit Noureddine (co-supervisors), ENSTA Alger.

---

## License

Released under the MIT License. See [`LICENSE`](LICENSE) for details.

---

## Résumé en français

**BatiSense Pro** est une plateforme IoT de supervision des bâtiments en temps réel. Des nœuds de capteurs (STM32, Arduino, ESP32-CAM) transmettent leurs mesures en LoRa 433 MHz vers une passerelle Raspberry Pi (Flask, SQLite, Mosquitto) qui alimente un tableau de bord web avec alertes (gaz, mouvement, vibrations), relevé automatique des compteurs d'eau et d'électricité, et historique des mesures. Projet de fin d'études, ENSTA Alger, 2026.
