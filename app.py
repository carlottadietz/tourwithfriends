import os
import sqlite3
import uuid
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
import xml.etree.ElementTree as ET

from flask import Flask, g, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "tour-with-friends-secret"),
    DATABASE_PATH=os.environ.get("DATABASE_PATH", str(Path(__file__).resolve().parent / "tourwithfriends.db")),
    UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER", str(Path(__file__).resolve().parent / "uploads")),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)


def init_db():
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    conn = sqlite3.connect(app.config["DATABASE_PATH"])
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            profile_image TEXT,
            total_distance_km REAL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            distance_km REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(app.config["DATABASE_PATH"])
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def allowed_image(filename):
    ext = Path(filename).suffix.lower()
    return ext in {".png", ".jpg", ".jpeg", ".webp"}


def allowed_gpx(filename):
    return Path(filename).suffix.lower() == ".gpx"


def haversine(lat1, lon1, lat2, lon2):
    radius = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return radius * c


def parse_gpx_distance(path):
    tree = ET.parse(path)
    root = tree.getroot()
    points = []
    for element in root.iter():
        if element.tag.endswith("trkpt"):
            lat = float(element.attrib.get("lat", "0"))
            lon = float(element.attrib.get("lon", "0"))
            points.append((lat, lon))
    if len(points) < 2:
        return 0.0

    total = 0.0
    previous = None
    for point in points:
        if previous is not None:
            total += haversine(previous[0], previous[1], point[0], point[1])
        previous = point
    return round(total, 2)


@app.route("/")
def index():
    user = get_current_user()
    leaderboard = get_db().execute(
        "SELECT id, name, profile_image, total_distance_km FROM users ORDER BY total_distance_km DESC, name ASC"
    ).fetchall()
    rides = []
    if user:
        rides = get_db().execute(
            "SELECT * FROM rides WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (user["id"],),
        ).fetchall()
    return render_template("index.html", user=user, leaderboard=leaderboard, rides=rides)


@app.route("/login", methods=["POST"])
def login():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("index"))

    profile_image = request.files.get("profile_image")
    filename = None
    if profile_image and profile_image.filename:
        if allowed_image(profile_image.filename):
            filename = secure_filename(profile_image.filename)
            filename = f"{uuid.uuid4().hex}_{filename}"
            profile_image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    conn = get_db()
    existing = conn.execute("SELECT id, profile_image FROM users WHERE name = ?", (name,)).fetchone()
    if existing:
        if filename and (existing["profile_image"] != filename):
            conn.execute("UPDATE users SET profile_image = ? WHERE id = ?", (filename, existing["id"]))
        user_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO users (name, profile_image, total_distance_km) VALUES (?, ?, 0)",
            (name, filename),
        )
        user_id = cur.lastrowid

    conn.commit()
    session["user_id"] = user_id
    session["user_name"] = name
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload():
    if not get_current_user():
        return redirect(url_for("index"))

    gpx_file = request.files.get("gpx_file")
    if not gpx_file or not gpx_file.filename or not allowed_gpx(gpx_file.filename):
        return redirect(url_for("index"))

    filename = secure_filename(gpx_file.filename)
    filename = f"{uuid.uuid4().hex}_{filename}"
    gpx_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    gpx_file.save(gpx_path)
    distance = parse_gpx_distance(gpx_path)

    conn = get_db()
    conn.execute(
        "INSERT INTO rides (user_id, filename, distance_km, created_at) VALUES (?, ?, ?, datetime('now'))",
        (session["user_id"], filename, distance),
    )
    conn.execute(
        "UPDATE users SET total_distance_km = total_distance_km + ? WHERE id = ?",
        (distance, session["user_id"]),
    )
    conn.commit()
    return redirect(url_for("index"))


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
