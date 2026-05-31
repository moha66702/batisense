"""
BatiSense Pro — Flask Backend with Authentication + Pi Token Auth
pip install flask flask-cors flask-login flask-bcrypt gunicorn
"""

import json
import sqlite3
import csv
import io
import time
import threading
import queue
import secrets
import os
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response, send_file, stream_with_context, redirect
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user, current_user
from flask_bcrypt import Bcrypt
from flask import send_from_directory

# ============================================================
#  App & extensions
# ============================================================
app = Flask(__name__)

def _cors_origins():
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return re.compile(r".*")

CORS(app, supports_credentials=True, origins=_cors_origins())

app.config["SECRET_KEY"]              = os.getenv("SECRET_KEY", "batisense_secret_key_123456")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE", "None")
app.config["SESSION_COOKIE_SECURE"]   = os.getenv("SESSION_COOKIE_SECURE", "True") == "True"

bcrypt        = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view    = "login_page"
login_manager.login_message = None

# Database stored in a persistent volume when deployed on Railway.
# DATA_DIR should point to a mounted volume such as /data.
DATA_DIR = os.getenv("DATA_DIR") or ("/data" if os.name != "nt" else ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.abspath(os.path.join(DATA_DIR, "batisense.db"))

# ============================================================
#  User Model
# ============================================================
class User(UserMixin):
    def __init__(self, row):
        (self.id, self.first_name, self.last_name, self.email,
         self.password, self.street, self.city, self.zip_code, self.created_at) = row

    def to_dict(self):
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "street": self.street,
            "city": self.city,
            "zip_code": self.zip_code,
            "created_at": self.created_at,
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
#  Database init — called at startup
# ============================================================
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                last_name  TEXT NOT NULL,
                email      TEXT NOT NULL UNIQUE,
                password   TEXT NOT NULL,
                street     TEXT,
                city       TEXT,
                zip_code   TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                token      TEXT NOT NULL UNIQUE,
                label      TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id     TEXT NOT NULL,
                sensor_type TEXT NOT NULL,
                value       REAL NOT NULL,
                timestamp   TEXT NOT NULL,
                user_id     INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id     TEXT NOT NULL,
                sensor_type TEXT NOT NULL,
                value       REAL NOT NULL,
                message     TEXT NOT NULL,
                level       TEXT NOT NULL DEFAULT 'warning',
                timestamp   TEXT NOT NULL,
                acked       INTEGER NOT NULL DEFAULT 0,
                user_id     INTEGER
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_readings_user ON readings   (user_id, node_id, sensor_type, timestamp)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user   ON alerts     (user_id, acked)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tokens        ON api_tokens (token)")
        con.commit()
    print("Database ready at:", DB_PATH)


def normalize_email(email):
    return str(email or "").strip().lower()


# ============================================================
#  Token helper
# ============================================================
def get_user_by_token(token):
    if not token:
        return None
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT user_id FROM api_tokens WHERE token=?", (token,)
        ).fetchone()
    return User.get_by_id(row[0]) if row else None


def require_token(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        auth  = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        user  = get_user_by_token(token)
        if not user:
            return jsonify({"error": "Invalid or missing token"}), 401
        request.token_user = user
        return f(*args, **kwargs)
    return decorated


# ============================================================
#  Alert thresholds
# ============================================================
THRESHOLDS = {
    "temperature":     {"min": 10, "max": 35,   "level": "warning"},
    "humidity":        {"min": 20, "max": 80,   "level": "warning"},
    "gas_detected":    {"eq": 1,                "level": "danger"},
    "structure_alert": {"eq": 1,                "level": "danger"},
    "door_open":       {"eq": 1,                "level": "info"},
    "water_meter":     {"min": 0, "max": 99999, "level": "info"},   # accepts any positive reading
}


def check_and_raise_alert(db, user_id, node_id, sensor_type, value, timestamp):
    rule = THRESHOLDS.get(sensor_type)
    if not rule:
        return
    triggered = (
        ("eq"  in rule and value == rule["eq"])  or
        ("max" in rule and value >  rule["max"]) or
        ("min" in rule and value <  rule["min"])
    )
    if not triggered:
        return
    if db.execute(
        "SELECT id FROM alerts WHERE user_id=? AND node_id=? AND sensor_type=? AND acked=0",
        (user_id, node_id, sensor_type)
    ).fetchone():
        return
    labels = {
        "gas_detected":    f"Gaz detecte dans {node_id}",
        "structure_alert": f"Alerte structure dans {node_id}",
        "door_open":       f"Porte ouverte ({node_id})",
        "temperature":     f"Temperature hors plage: {value}C ({node_id})",
        "humidity":        f"Humidite hors plage: {value}% ({node_id})",
    }
    message = labels.get(sensor_type, f"{sensor_type}={value} sur {node_id}")
    level   = rule.get("level", "warning")
    cur = db.execute(
        "INSERT INTO alerts (node_id,sensor_type,value,message,level,timestamp,user_id) VALUES (?,?,?,?,?,?,?)",
        (node_id, sensor_type, value, message, level, timestamp, user_id)
    )
    db.commit()
    sse_push_to_user(user_id, "alert", {
        "id": cur.lastrowid, "node_id": node_id, "sensor_type": sensor_type,
        "value": value, "message": message, "level": level, "timestamp": timestamp
    })


# ============================================================
#  SSE — per user
# ============================================================
_subscribers = {}
_sub_lock = threading.Lock()
_SSE_QUEUE_MAX = 1000


def sse_push_to_user(user_id, event, data):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sub_lock:
        for q in _subscribers.get(user_id, []):
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except queue.Empty:
                    pass


# ============================================================
#  AUTH ROUTES
# ============================================================
@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    for field in ["first_name", "last_name", "email", "password", "street", "city", "zip_code"]:
        if not str(data.get(field, "")).strip():
            return jsonify({"error": f"Le champ '{field}' est obligatoire."}), 400
    email = normalize_email(data["email"])
    if len(data["password"]) < 8:
        return jsonify({"error": "Le mot de passe doit contenir au moins 8 caracteres."}), 400
    if User.get_by_email(email):
        return jsonify({"error": "Un compte avec cet e-mail existe deja."}), 409
    hashed = bcrypt.generate_password_hash(data["password"]).decode("utf-8")
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO users (first_name,last_name,email,password,street,city,zip_code) VALUES (?,?,?,?,?,?,?)",
            (data["first_name"].strip(), data["last_name"].strip(), email, hashed,
             data["street"].strip(), data["city"].strip(), data["zip_code"].strip())
        )
        new_id = cur.lastrowid
        con.commit()
    user = User.get_by_id(new_id)
    login_user(user, remember=True)
    return jsonify({"message": "Compte cree.", "user": user.to_dict()}), 201


@app.route("/auth/login", methods=["POST"])
def login():
    data     = request.get_json(silent=True) or {}
    email    = normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    user     = User.get_by_email(email)
    if not user or not bcrypt.check_password_hash(user.password, password):
        return jsonify({"error": "E-mail ou mot de passe incorrect."}), 401
    login_user(user, remember=True)
    return jsonify({"message": "Connexion reussie.", "user": user.to_dict()}), 200


@app.route("/auth/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Deconnexion reussie."}), 200


@app.route("/auth/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    current_password = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", ""))

    if not current_password or not new_password:
        return jsonify({"error": "Mot de passe actuel et nouveau mot de passe requis."}), 400
    if len(new_password) < 8:
        return jsonify({"error": "Le nouveau mot de passe doit contenir au moins 8 caracteres."}), 400
    if not bcrypt.check_password_hash(current_user.password, current_password):
        return jsonify({"error": "Mot de passe actuel incorrect."}), 401

    hashed = bcrypt.generate_password_hash(new_password).decode("utf-8")
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE users SET password=? WHERE id=?",
            (hashed, current_user.id)
        )
        con.commit()
    return jsonify({"message": "Mot de passe mis a jour."}), 200


@app.route("/auth/me")
@login_required
def me():
    return jsonify({"user": current_user.to_dict()})


@app.route('/batisense-logo.svg')
def serve_logo():
    return send_from_directory('.', 'batisense-logo.svg')

@app.route('/batisense-icon.svg')
def serve_icon():
    return send_from_directory('.', 'batisense-icon.svg')


# ============================================================
#  TOKEN MANAGEMENT
# ============================================================
@app.route("/auth/tokens", methods=["GET"])
@login_required
def list_tokens():
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, token, label, created_at FROM api_tokens WHERE user_id=? ORDER BY created_at DESC",
            (current_user.id,)
        ).fetchall()
    return jsonify([{"id": r[0], "token": r[1], "label": r[2], "created_at": r[3]} for r in rows])


@app.route("/auth/tokens", methods=["POST"])
@login_required
def create_token():
    data  = request.get_json(silent=True) or {}
    label = data.get("label", "Raspberry Pi")
    token = secrets.token_hex(32)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO api_tokens (user_id, token, label) VALUES (?,?,?)",
            (current_user.id, token, label)
        )
        con.commit()
    return jsonify({"token": token, "label": label}), 201


@app.route("/auth/tokens/<int:token_id>", methods=["DELETE"])
@login_required
def delete_token(token_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM api_tokens WHERE id=? AND user_id=?",
            (token_id, current_user.id)
        )
        con.commit()
    return jsonify({"ok": True})


# ============================================================
#  Pi LOGIN — Pi calls this once at startup
# ============================================================
@app.route("/api/pi/login", methods=["POST"])
def pi_login():
    data     = request.get_json(silent=True) or {}
    email    = normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    label    = str(data.get("label", "Raspberry Pi"))
    user     = User.get_by_email(email)
    if not user or not bcrypt.check_password_hash(user.password, password):
        return jsonify({"error": "E-mail ou mot de passe incorrect."}), 401
    with sqlite3.connect(DB_PATH) as con:
        existing = con.execute(
            "SELECT token FROM api_tokens WHERE user_id=? AND label=?",
            (user.id, label)
        ).fetchone()
        if existing:
            return jsonify({"token": existing[0], "user_id": user.id, "reused": True})
        token = secrets.token_hex(32)
        con.execute(
            "INSERT INTO api_tokens (user_id, token, label) VALUES (?,?,?)",
            (user.id, token, label)
        )
        con.commit()
    return jsonify({"token": token, "user_id": user.id, "reused": False}), 201


# ============================================================
#  Pi DATA — Pi sends sensor readings
# ============================================================
@app.route("/api/pi/data", methods=["POST"])
@require_token
def pi_receive_data():
    user      = request.token_user
    payload   = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({"error": "bad JSON"}), 400
    node_id   = payload.get("node_id")
    timestamp = payload.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if not node_id:
        return jsonify({"error": "missing node_id"}), 400
    skip    = {"node_id", "timestamp"}
    sensors = {k: v for k, v in payload.items() if k not in skip and isinstance(v, (int, float))}
    if not sensors:
        return jsonify({"error": "no sensor values"}), 400
    db = sqlite3.connect(DB_PATH)
    try:
        for sensor_type, value in sensors.items():
            db.execute(
                "INSERT INTO readings (node_id,sensor_type,value,timestamp,user_id) VALUES (?,?,?,?,?)",
                (node_id, sensor_type, float(value), timestamp, user.id)
            )
            sse_push_to_user(user.id, "sensor", {
                "node_id": node_id, "sensor_type": sensor_type,
                "value": value, "timestamp": timestamp
            })
            check_and_raise_alert(db, user.id, node_id, sensor_type, value, timestamp)
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "node_id": node_id, "user": user.first_name, "sensors": list(sensors.keys())}), 201


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
    return send_file("dashboard.html")


# ============================================================
#  DASHBOARD API ROUTES
# ============================================================
@app.route("/api/latest")
@login_required
def get_latest():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT node_id, sensor_type, value, timestamp FROM readings r
        WHERE user_id=? AND id=(
            SELECT r2.id FROM readings r2
            WHERE r2.user_id=r.user_id AND r2.node_id=r.node_id AND r2.sensor_type=r.sensor_type
            ORDER BY r2.timestamp DESC, r2.id DESC
            LIMIT 1
        )
        ORDER BY node_id, sensor_type
    """, (current_user.id,)).fetchall()
    db.close()
    result = {}
    for row in rows:
        nid = row["node_id"]
        if nid not in result:
            result[nid] = {}
        result[nid][row["sensor_type"]] = {"value": row["value"], "timestamp": row["timestamp"]}
    return jsonify(result)


@app.route("/api/averages")
@login_required
def get_averages():
    db = sqlite3.connect(DB_PATH)
    def lv(node, sensor):
        r = db.execute(
            "SELECT value FROM readings WHERE user_id=? AND node_id=? AND sensor_type=? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (current_user.id, node, sensor)
        ).fetchone()
        return r[0] if r else None
    kt, st = lv("kitchen", "temperature"), lv("salon", "temperature")
    kh, sh = lv("kitchen", "humidity"),    lv("salon", "humidity")
    db.close()
    def avg(a, b):
        vs = [x for x in [a, b] if x is not None]
        return round(sum(vs) / len(vs), 1) if vs else None
    return jsonify({
        "avg_temperature": avg(kt, st), "avg_humidity": avg(kh, sh),
        "sources": {"kitchen_temperature": kt, "salon_temperature": st,
                    "kitchen_humidity": kh, "salon_humidity": sh}
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
    rows = db.execute(
        "SELECT timestamp as t, value as v FROM readings "
        "WHERE user_id=? AND node_id=? AND sensor_type=? AND timestamp>=? ORDER BY timestamp ASC, id ASC",
        (current_user.id, node_id, sensor_type, since)
    ).fetchall()
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
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE alerts SET acked=1 WHERE id=? AND user_id=?", (alert_id, current_user.id))
        con.commit()
    return jsonify({"ok": True})


@app.route("/api/export/csv")
@login_required
def export_csv():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT node_id,sensor_type,value,timestamp FROM readings "
        "WHERE user_id=? ORDER BY timestamp DESC, id DESC LIMIT 50000",
        (current_user.id,)
    ).fetchall()
    db.close()
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["node_id", "sensor_type", "value", "timestamp"])
    for r in rows:
        w.writerow([r["node_id"], r["sensor_type"], r["value"], r["timestamp"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=batisense_export.csv"})


@app.route("/api/stream")
@login_required
def sse_stream():
    uid = current_user.id
    q   = queue.Queue(maxsize=_SSE_QUEUE_MAX)
    with _sub_lock:
        _subscribers.setdefault(uid, []).append(q)

    def generate():
        last_ping = time.time()
        try:
            yield "event: ping\ndata: {}\n\n"
            while True:
                try:
                    yield q.get(timeout=15)
                    last_ping = time.time()
                    while True:
                        yield q.get_nowait()
                except queue.Empty:
                    if time.time() - last_ping >= 15:
                        yield "event: ping\ndata: {}\n\n"
                        last_ping = time.time()
        except GeneratorExit:
            pass
        finally:
            with _sub_lock:
                if uid in _subscribers and q in _subscribers[uid]:
                    _subscribers[uid].remove(q)
                    if not _subscribers[uid]:
                        del _subscribers[uid]

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "subscribers": sum(len(v) for v in _subscribers.values())})


# ============================================================
#  WATER METER — dedicated read endpoint (public for quick check)
# ============================================================
@app.route("/api/water_meter/latest")
@login_required
def water_meter_latest():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT value, timestamp FROM readings "
        "WHERE user_id=? AND node_id='water_meter' AND sensor_type='water_meter' "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (current_user.id,)
    ).fetchone()
    db.close()
    if not row:
        return jsonify({"node": "water_meter", "value": None, "unit": "m³", "timestamp": None})
    return jsonify({
        "node":      "water_meter",
        "value":     row["value"],
        "unit":      "m³",
        "timestamp": row["timestamp"]
    })


# ============================================================
#  BOOT — init DB then start server
# ============================================================
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("=" * 52)
    print("  BatiSense Pro")
    print(f"  http://0.0.0.0:{port}")
    print("=" * 52)
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
