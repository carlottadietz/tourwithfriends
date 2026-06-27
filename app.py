import os
import sqlite3
import uuid
from datetime import datetime, timezone
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
            total_distance_km REAL DEFAULT 0,
            total_elevation_m REAL DEFAULT 0,
            total_duration_min REAL DEFAULT 0
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
            elevation_m REAL DEFAULT 0,
            duration_min REAL DEFAULT 0,
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


def parse_gpx_metrics(path):
    tree = ET.parse(path)
    root = tree.getroot()
    points = []
    for element in root.iter():
        if element.tag.endswith("trkpt"):
            lat = float(element.attrib.get("lat", "0"))
            lon = float(element.attrib.get("lon", "0"))
            ele = None
            time_text = None
            for child in element:
                if child.tag.endswith("ele"):
                    ele = float(child.text or 0)
                elif child.tag.endswith("time"):
                    time_text = child.text
            points.append({"lat": lat, "lon": lon, "ele": ele or 0, "time": time_text})

    if len(points) < 2:
        return {"distance_km": 0.0, "elevation_m": 0.0, "duration_min": 0.0}

    total_distance = 0.0
    total_elevation = 0.0
    previous = None
    start_time = None
    end_time = None
    for point in points:
        if previous is not None:
            total_distance += haversine(previous["lat"], previous["lon"], point["lat"], point["lon"])
            delta_elevation = point["ele"] - previous["ele"]
            if delta_elevation > 0:
                total_elevation += delta_elevation
        previous = point

        if point["time"]:
            try:
                parsed_time = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
            except ValueError:
                parsed_time = None
            if parsed_time is not None:
                if start_time is None:
                    start_time = parsed_time
                end_time = parsed_time

    if start_time and end_time:
        duration_minutes = max((end_time - start_time).total_seconds() / 60.0, 0.0)
    else:
        duration_minutes = 0.0

    return {
        "distance_km": round(total_distance, 2),
        "elevation_m": round(total_elevation, 2),
        "duration_min": round(duration_minutes, 2),
    }


@app.route("/")
def index():
    user = get_current_user()
    conn = get_db()
    users = conn.execute("SELECT id, name FROM users ORDER BY name ASC").fetchall()
    distance_leaderboard = conn.execute(
        "SELECT id, name, profile_image, total_distance_km FROM users ORDER BY total_distance_km DESC, name ASC"
    ).fetchall()
    elevation_leaderboard = conn.execute(
        "SELECT id, name, profile_image, total_elevation_m FROM users ORDER BY total_elevation_m DESC, name ASC"
    ).fetchall()
    duration_leaderboard = conn.execute(
        "SELECT id, name, profile_image, total_duration_min FROM users ORDER BY total_duration_min DESC, name ASC"
    ).fetchall()
    daily_winners = conn.execute(
        """
        SELECT r.created_at as day, r.user_id, r.distance_km, u.name as user_name
        FROM rides r
        JOIN users u ON r.user_id = u.id
        ORDER BY date(r.created_at) DESC, r.distance_km DESC
        """
    ).fetchall()
    rides = []
    if user:
        rides = conn.execute(
            "SELECT * FROM rides WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (user["id"],),
        ).fetchall()

    current_leader = None
    if distance_leaderboard:
        current_leader = distance_leaderboard[0]

    stats = {
        "distance": sum(entry["total_distance_km"] for entry in distance_leaderboard),
        "elevation": sum(entry["total_elevation_m"] for entry in elevation_leaderboard),
        "duration": sum(entry["total_duration_min"] for entry in duration_leaderboard),
    }

    return render_template(
        "index.html",
        user=user,
        users=users,
        current_leader=current_leader,
        stats=stats,
        distance_leaderboard=distance_leaderboard,
        elevation_leaderboard=elevation_leaderboard,
        duration_leaderboard=duration_leaderboard,
        daily_winners=daily_winners,
        rides=rides,
    )


@app.route("/login", methods=["POST"])
def login():
    selected_name = request.form.get("existing_user", "").strip()
    name = request.form.get("name", "").strip()
    if selected_name:
        name = selected_name
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
    distance = parse_gpx_metrics(gpx_path)

    conn = get_db()
    conn.execute(
        "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (
            session["user_id"],
            filename,
            distance["distance_km"],
            distance["elevation_m"],
            distance["duration_min"],
        ),
    )
    conn.execute(
        "UPDATE users SET total_distance_km = total_distance_km + ?, total_elevation_m = total_elevation_m + ?, total_duration_min = total_duration_min + ? WHERE id = ?",
        (
            distance["distance_km"],
            distance["elevation_m"],
            distance["duration_min"],
            session["user_id"],
        ),
    )
    conn.commit()
    return redirect(url_for("index"))


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
