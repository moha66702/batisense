# BatiSense Pro

**Real-time building monitoring platform: sensor data in, live dashboard and alerts out.**

![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-backend-000000?logo=flask)
![SQLite](https://img.shields.io/badge/SQLite-database-003B57?logo=sqlite&logoColor=white)
![Chart.js](https://img.shields.io/badge/Chart.js-4.4-FF6384?logo=chartdotjs&logoColor=white)
![LoRa](https://img.shields.io/badge/LoRa-433%20MHz-00A6D6)

BatiSense Pro is an IoT platform that monitors temperature, humidity, gas, motion, vibration, door state, and water, electricity and gas consumption in a building. Sensor nodes send their data over LoRa to a Raspberry Pi gateway, which forwards it to this web platform. The platform stores the readings, raises alerts and streams everything live to a dashboard.

Built as an end-of-studies project (PFE) at **ENSTA Alger** (Telecommunications & Networks) and entered in the **Startup DZ** competition.

> This repository contains the **web platform** (Flask backend and dashboard). The embedded firmware and the gateway script are not included here.

---

## Features

- **User accounts**: registration, login, password change (bcrypt hashing, Flask-Login sessions).
- **Per-user API tokens** for the Raspberry Pi gateway, so each user only sees their own data.
- **Live dashboard** updated through Server-Sent Events (no page refresh).
- **Automatic alerts** with severity levels (see thresholds below), with acknowledgement.
- **History and charts** (Chart.js): temperature, humidity, presence, water, electricity and gas.
- **Water meter** endpoint for readings coming from the camera/OCR node.
- **CSV export** of all readings.
- Database stored in SQLite, one file, no extra service to install.

---

## System overview

```
 Sensor nodes ──LoRa 433 MHz──▶ Raspberry Pi gateway ──HTTPS + token──▶ BatiSense Pro (Flask)
 (STM32, Arduino, ESP32-CAM)    (receives and forwards)                      │
                                                                             ├─ SQLite (readings, alerts, users)
                                                                             └─ SSE ──▶ Web dashboard
```

| Node | Board | Sensors |
|---|---|---|
| Kitchen | STM32 Nucleo F446RE | DHT11, MQ-4 (gas), PIR, SW-420 (vibration), SX1278 LoRa |
| Electricity | Arduino Uno | Optical pulse detection on the electricity meter |
| Water | ESP32-CAM | Camera with OCR for water-meter reading |

LoRa settings: 433 MHz, SF7, BW 125 kHz, coding rate 4/5, sync word `0x34`.

---

## Alert thresholds

| Sensor | Rule | Level |
|---|---|---|
| Temperature | below 10 °C or above 35 °C | warning |
| Humidity | below 20 % or above 80 % | warning |
| Gas detected | value = 1 | danger |
| Structure / vibration alert | value = 1 | danger |
| Door open | value = 1 | info |

---

## Tech stack

- **Backend:** Python, Flask, Flask-Login, Flask-Bcrypt, Flask-CORS, SQLite, Gunicorn
- **Frontend:** HTML, CSS, JavaScript, Chart.js
- **Real-time:** Server-Sent Events

---

## Project structure

```
batisense/
├── app.py               # Flask app: auth, Pi API, alerts, SSE, export
├── index.html           # Main dashboard
├── auth.html            # Login / registration page
├── admin.html           # Admin page
├── admin_login.html     # Admin login page
├── batisense-logo.svg
├── batisense-icon.svg
├── requirements.txt
├── Procfile             # Gunicorn start command for PaaS hosting
└── README.md
```

---

## Getting started

### 1. Clone and install

```bash
git clone https://github.com/moha66702/batisense.git
cd batisense
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure (environment variables)

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | built-in dev value | **Set your own long random value in production** |
| `DATA_DIR` | `/data` (Linux/macOS), `.` (Windows) | Folder for the SQLite database |
| `CORS_ORIGINS` | all origins | Comma-separated list of allowed origins |
| `SESSION_COOKIE_SECURE` | `True` | Set to `False` for local HTTP testing |
| `SESSION_COOKIE_SAMESITE` | `None` | Use `Lax` for local testing |
| `PORT` | `8080` | Port used by `python app.py` |

Local example:

```bash
export SECRET_KEY="change-me-to-a-long-random-string"
export DATA_DIR="."
export SESSION_COOKIE_SECURE="False"
export SESSION_COOKIE_SAMESITE="Lax"
```

### 3. Run

```bash
python app.py
# or, like in production:
gunicorn app:app --workers 2 --threads 4 --timeout 120
```

Open `http://localhost:8080`, create an account, then log in.

---

## API for the Raspberry Pi gateway

**1. Get a token (once, at startup)**

```bash
curl -X POST http://localhost:8080/api/pi/login \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "your-password", "label": "Raspberry Pi"}'
```

**2. Send sensor readings**

```bash
curl -X POST http://localhost:8080/api/pi/data \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"node_id": "kitchen", "temperature": 24.5, "humidity": 48, "gas_detected": 0}'
```

Every numeric field except `node_id` and `timestamp` is stored as a reading. `timestamp` is optional (ISO 8601, defaults to server time).

### Main endpoints

| Method | Route | Description |
|---|---|---|
| POST | `/auth/register`, `/auth/login`, `/auth/logout` | Account management |
| POST | `/auth/change-password` | Change password |
| GET / POST / DELETE | `/auth/tokens` | List, create, delete API tokens |
| POST | `/api/pi/login` | Gateway login, returns a token |
| POST | `/api/pi/data` | Gateway sends readings (token required) |
| GET | `/api/latest` | Latest value per sensor |
| GET | `/api/averages` | Averages |
| GET | `/api/history` | Historical readings |
| GET | `/api/alerts` | List alerts |
| POST | `/api/alerts/<id>/ack` | Acknowledge an alert |
| GET | `/api/stream` | Live events (SSE) |
| GET | `/api/export/csv` | Export readings as CSV |
| GET | `/api/water_meter/latest` | Latest water-meter reading |
| GET | `/api/health` | Health check |

---

## Authors

- **Djelloul Mohammed El Hacene**: [GitHub](https://github.com/moha66702) · [LinkedIn](https://linkedin.com/in/djelloul-mohammed-805b79394)
- **Kouachi Aissam**

Supervision: Boudiar Toufik (supervisor), Beghami Sami and Cherabit Noureddine (co-supervisors), ENSTA Alger.

---

## Résumé en français

**BatiSense Pro** est une plateforme IoT de supervision des bâtiments en temps réel. Des nœuds de capteurs (STM32, Arduino, ESP32-CAM) transmettent leurs mesures en LoRa vers une passerelle Raspberry Pi, qui les envoie à cette application Flask. La plateforme gère les comptes utilisateurs, enregistre les mesures dans SQLite, déclenche des alertes (gaz, vibrations, température, humidité, porte) et alimente en direct un tableau de bord web avec historique, graphiques et export CSV. Projet de fin d'études, ENSTA Alger, 2026.
