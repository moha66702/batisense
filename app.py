"""
BatiSense Pro — Flask Backend with Authentication
Run:  pip install flask flask-cors flask-login flask-bcrypt
      python app.py
"""

import json
import sqlite3
import csv
import io
import time
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response, send_file, stream_with_context, session, redirect
from flask_cors import CORS
from flask_login import (
    LoginManager, UserMixin, login_required, login_user, logout_user, current_user
)
from flask_bcrypt import Bcrypt
import os

# ============================================================
#  App & extensions
# ============================================================
app = Flask(__name__)

# Allow requests from the same server (both with and without www, http)
CORS(app, supports_credentials=True, origins=[
    "http://10.203.84.58:5000",
    "http://localhost:5000",
    "http://127.0.0.1:5000",
])

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change_this_in_production_12345")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set to True only if you use HTTPS:
app.config["SESSION_COOKIE_SECURE"] = False

bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "login_page"
login_manager.login_message = None

DB_PATH = "batisense.db"

# ============================================================
#  User Model & loader
# ============================================================
class User(UserMixin):
    def __init__(self, row):
        (self.id, self.first_name, self.last_name, self.email,
         self.password, self.street, self.city, self.zip_code, self.created_at) = row

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def to_dict(self):
        return {
            "id": self.id, "first_name": self.first_name, "last_name": self.last_name,
            "email": self.email, "street": self.street, "city": self.city,
            "zip_code": self.zip_code, "created_at": self.created_at,
        }

    @staticmethod
    def get_by_id(user_id):
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT id,first_name,last_name,email,password,street,city,zip_code,created_at "
                "FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return User(row) if row else None

    @staticmethod
    def get_by_email(email):
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT id,first_name,last_name,email,password,street,city,zip_code,created_at "
                "FROM users WHERE LOWER(email)=LOWER(?)", (email,)
            ).fetchone()
        return User(row) if row else None

@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(int(user_id))

# ============================================================
#  Database init
# ============================================================
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT    NOT NULL,
                last_name  TEXT    NOT NULL,
                email      TEXT    NOT NULL UNIQUE,
                password   TEXT    NOT NULL,
                street     TEXT,
                city       TEXT,
                zip_code   TEXT,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id     TEXT    NOT NULL,
                sensor_type TEXT    NOT NULL,
                value       REAL    NOT NULL,
                timestamp   TEXT    NOT NULL
            )
        """)
        try:
            con.execute("ALTER TABLE readings ADD COLUMN user_id INTEGER")
            print("→ Added user_id column to readings")
        except sqlite3.OperationalError:
            pass

        con.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id     TEXT NOT NULL,
                sensor_type TEXT NOT NULL,
                value       REAL NOT NULL,
                message     TEXT NOT NULL,
                level       TEXT NOT NULL DEFAULT 'warning',
                timestamp   TEXT NOT NULL,
                acked       INTEGER NOT NULL DEFAULT 0
            )
        """)
        try:
            con.execute("ALTER TABLE alerts ADD COLUMN user_id INTEGER")
            print("→ Added user_id column to alerts")
        except sqlite3.OperationalError:
            pass

        con.execute("CREATE INDEX IF NOT EXISTS idx_readings_user_ts ON readings (user_id, node_id, sensor_type, timestamp)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user_acked ON alerts (user_id, acked)")
        con.commit()

# ============================================================
#  Alert thresholds
# ============================================================
THRESHOLDS = {
    "temperature":       {"min": 10,  "max": 35,   "level": "warning"},
    "humidity":          {"min": 20,  "max": 80,   "level": "warning"},
    "gas_detected":      {"eq":  1,               "level": "danger"},
    "structure_alert":   {"eq":  1,               "level": "danger"},
    "door_open":         {"eq":  1,               "level": "info"},
}

def check_and_raise_alert(db, user_id, node_id, sensor_type, value, timestamp):
    rule = THRESHOLDS.get(sensor_type)
    if not rule:
        return

    triggered = False
    if "eq" in rule and value == rule["eq"]:
        triggered = True
    if "max" in rule and value > rule["max"]:
        triggered = True
    if "min" in rule and value < rule["min"]:
        triggered = True
    if not triggered:
        return

    existing = db.execute(
        "SELECT id FROM alerts WHERE user_id=? AND node_id=? AND sensor_type=? AND acked=0",
        (user_id, node_id, sensor_type)
    ).fetchone()
    if existing:
        return

    labels = {
        "gas_detected":    f"⚠️ Gaz détecté dans {node_id}",
        "structure_alert": f"⚠️ Alerte structure dans {node_id}",
        "door_open":       f"🚪 Porte ouverte ({node_id})",
        "temperature":     f"Température hors plage: {value}°C ({node_id})",
        "humidity":        f"Humidité hors plage: {value}% ({node_id})",
    }
    message = labels.get(sensor_type, f"{sensor_type}={value} sur {node_id}")
    level   = rule.get("level", "warning")

    cur = db.execute(
        "INSERT INTO alerts (node_id, sensor_type, value, message, level, timestamp, user_id) VALUES (?,?,?,?,?,?,?)",
        (node_id, sensor_type, value, message, level, timestamp, user_id)
    )
    db.commit()
    # Push alert only to the relevant user's SSE queue
    sse_push_to_user(user_id, "alert", {
        "id": cur.lastrowid, "node_id": node_id, "sensor_type": sensor_type,
        "value": value, "message": message, "level": level, "timestamp": timestamp
    })

# ============================================================
#  SSE subscribers — keyed by user_id so data is never mixed
# ============================================================
# _subscribers = { user_id: [ [queue1], [queue2], ... ] }
_subscribers: dict[int, list[list]] = {}
_sub_lock = threading.Lock()

def sse_push_to_user(user_id: int, event: str, data: dict):
    """Push an SSE message only to queues belonging to user_id."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sub_lock:
        for q in _subscribers.get(user_id, []):
            q.append(msg)

# ============================================================
#  AUTH ROUTES
# ============================================================
@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    required = ["first_name", "last_name", "email", "password", "street", "city", "zip_code"]
    for field in required:
        if not str(data.get(field, "")).strip():
            return jsonify({"error": f"Le champ '{field}' est obligatoire."}), 400

    email = data["email"].strip().lower()
    password = data["password"]

    if len(password) < 8:
        return jsonify({"error": "Le mot de passe doit contenir au moins 8 caractères."}), 400

    if User.get_by_email(email):
        return jsonify({"error": "Un compte avec cet e-mail existe déjà."}), 409

    hashed = bcrypt.generate_password_hash(password).decode("utf-8")
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO users (first_name,last_name,email,password,street,city,zip_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (data["first_name"].strip(), data["last_name"].strip(),
             email, hashed,
             data["street"].strip(), data["city"].strip(), data["zip_code"].strip())
        )
        new_id = cur.lastrowid
        con.commit()

    user = User.get_by_id(new_id)
    login_user(user, remember=True)
    return jsonify({"message": "Compte créé avec succès.", "user": user.to_dict()}), 201

@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    user = User.get_by_email(email)
    if not user or not bcrypt.check_password_hash(user.password, password):
        return jsonify({"error": "E-mail ou mot de passe incorrect."}), 401

    login_user(user, remember=True)
    return jsonify({"message": "Connexion réussie.", "user": user.to_dict()}), 200

@app.route("/auth/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Déconnexion réussie."}), 200

@app.route("/auth/me", methods=["GET"])
@login_required
def me():
    return jsonify({"user": current_user.to_dict()}), 200

# ============================================================
#  PAGE ROUTES
# ============================================================
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect("/dashboard")
    return redirect("/login")

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect("/dashboard")
    return send_file("auth.html")

@app.route("/dashboard")
@login_required
def dashboard():
    return send_file("index.html")

# ============================================================
#  API ROUTES (protected, filtered by current_user.id)
# ============================================================

@app.route("/api/data", methods=["POST"])
def receive_data():
    """Receive data from Raspberry Pi. Requires user_id in JSON payload."""
    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({"error": "bad JSON"}), 400

    user_id = payload.get("user_id")
    if not user_id:
        return jsonify({"error": "missing user_id"}), 400

    with sqlite3.connect(DB_PATH) as con:
        exists = con.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
    if not exists:
        return jsonify({"error": "invalid user_id"}), 400

    node_id   = payload.get("node_id")
    timestamp = payload.get("timestamp") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if not node_id:
        return jsonify({"error": "missing node_id"}), 400

    skip = {"node_id", "timestamp", "user_id"}
    sensors = {k: v for k, v in payload.items() if k not in skip and isinstance(v, (int, float))}

    if not sensors:
        return jsonify({"error": "no sensor values"}), 400

    db = sqlite3.connect(DB_PATH)
    try:
        for sensor_type, value in sensors.items():
            db.execute(
                "INSERT INTO readings (node_id, sensor_type, value, timestamp, user_id) VALUES (?,?,?,?,?)",
                (node_id, sensor_type, float(value), timestamp, user_id)
            )
            # Push sensor data only to the correct user's SSE stream
            sse_push_to_user(user_id, "sensor", {
                "node_id": node_id, "sensor_type": sensor_type,
                "value": value, "timestamp": timestamp, "user_id": user_id
            })
            check_and_raise_alert(db, user_id, node_id, sensor_type, value, timestamp)
        db.commit()
    finally:
        db.close()

    return jsonify({"ok": True, "node_id": node_id, "sensors": list(sensors.keys())}), 201

@app.route("/api/latest")
@login_required
def get_latest():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT node_id, sensor_type, value, timestamp
        FROM readings r
        WHERE user_id = ? AND timestamp = (
            SELECT MAX(r2.timestamp) FROM readings r2
            WHERE r2.user_id = r.user_id AND r2.node_id = r.node_id AND r2.sensor_type = r.sensor_type
        )
        ORDER BY node_id, sensor_type
    """, (current_user.id,)).fetchall()
    db.close()

    result = {}
    for row in rows:
        nid = row["node_id"]
        if nid not in result:
            result[nid] = {}
        result[nid][row["sensor_type"]] = {
            "value": row["value"],
            "timestamp": row["timestamp"]
        }
    return jsonify(result)

@app.route("/api/averages")
@login_required
def get_averages():
    db = sqlite3.connect(DB_PATH)
    def latest_val(node, sensor):
        row = db.execute("""
            SELECT value FROM readings
            WHERE user_id=? AND node_id=? AND sensor_type=?
            ORDER BY timestamp DESC LIMIT 1
        """, (current_user.id, node, sensor)).fetchone()
        return row[0] if row else None

    kt = latest_val("kitchen", "temperature")
    st = latest_val("salon", "temperature")
    kh = latest_val("kitchen", "humidity")
    sh = latest_val("salon", "humidity")
    db.close()

    def avg(a, b):
        vs = [x for x in [a, b] if x is not None]
        return round(sum(vs) / len(vs), 1) if vs else None

    return jsonify({
        "avg_temperature": avg(kt, st),
        "avg_humidity":    avg(kh, sh),
        "sources": {
            "kitchen_temperature": kt, "salon_temperature": st,
            "kitchen_humidity": kh, "salon_humidity": sh,
        }
    })

@app.route("/api/history")
@login_required
def get_history():
    node_id     = request.args.get("node_id")
    sensor_type = request.args.get("sensor_type")
    hours       = int(request.args.get("hours", 24))
    if not node_id or not sensor_type:
        return jsonify({"error": "node_id and sensor_type required"}), 400

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT timestamp as t, value as v FROM readings
        WHERE user_id=? AND node_id=? AND sensor_type=? AND timestamp >= ?
        ORDER BY timestamp ASC
    """, (current_user.id, node_id, sensor_type, since)).fetchall()
    db.close()
    return jsonify([{"t": r["t"], "v": r["v"]} for r in rows])

@app.route("/api/alerts")
@login_required
def get_alerts():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM alerts WHERE user_id=? AND acked=0 ORDER BY timestamp DESC LIMIT 100",
        (current_user.id,)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/<int:alert_id>/ack", methods=["POST"])
@login_required
def ack_alert(alert_id):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "UPDATE alerts SET acked=1 WHERE id=? AND user_id=?",
        (alert_id, current_user.id)
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})

@app.route("/api/export/csv")
@login_required
def export_csv():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT node_id, sensor_type, value, timestamp FROM readings WHERE user_id=? ORDER BY timestamp DESC LIMIT 50000",
        (current_user.id,)
    ).fetchall()
    db.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["node_id", "sensor_type", "value", "timestamp"])
    for r in rows:
        w.writerow([r["node_id"], r["sensor_type"], r["value"], r["timestamp"]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=batisense_export.csv"}
    )

@app.route("/api/stream")
@login_required
def sse_stream():
    uid = current_user.id
    q = []

    with _sub_lock:
        if uid not in _subscribers:
            _subscribers[uid] = []
        _subscribers[uid].append(q)

    def generate():
        last_ping = time.time()
        try:
            yield "event: ping\ndata: {}\n\n"
            while True:
                if q:
                    while q:
                        yield q.pop(0)
                else:
                    if time.time() - last_ping > 15:
                        yield "event: ping\ndata: {}\n\n"
                        last_ping = time.time()
                    time.sleep(0.05)
        except GeneratorExit:
            pass
        finally:
            with _sub_lock:
                if uid in _subscribers and q in _subscribers[uid]:
                    _subscribers[uid].remove(q)
                    if not _subscribers[uid]:
                        del _subscribers[uid]

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "subscribers": sum(len(v) for v in _subscribers.values())})

# ============================================================
#  BOOT
# ============================================================
if __name__ == "__main__":
    init_db()
    print("=" * 52)
    print("  BatiSense Pro — with Authentication")
    print("  http://0.0.0.0:5000")
    print("=" * 52)
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)