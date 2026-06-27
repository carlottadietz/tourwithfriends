import os
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
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
    PERMANENT_SESSION_LIFETIME=timedelta(days=40),
)


def init_db():
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    conn = sqlite3.connect(app.config["DATABASE_PATH"])
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            gender TEXT NOT NULL DEFAULT 'Homme',
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
    columns = conn.execute("PRAGMA table_info(users)").fetchall()
    column_names = {col[1] for col in columns}
    if "gender" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN gender TEXT NOT NULL DEFAULT 'Homme'")
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


PAUSE_THRESHOLD_SECONDS = 1800
ELEVATION_HYSTERESIS_M = 4.5
EVENT_START_MONTH_DAY = (7, 4)
VALID_GENDERS = ("Femme", "Homme")


def calculate_ascent_hysteresis(elevations, threshold_m=ELEVATION_HYSTERESIS_M):
    if not elevations:
        return 0.0

    accepted = [elevations[0]]
    last = elevations[0]
    for value in elevations[1:]:
        if abs(value - last) >= threshold_m:
            accepted.append(value)
            last = value

    total_ascent = 0.0
    for previous, current in zip(accepted, accepted[1:]):
        if current > previous:
            total_ascent += current - previous

    return total_ascent


def is_allowed_event_date(created_at_iso):
    try:
        activity_date = datetime.fromisoformat(created_at_iso).date()
    except ValueError:
        return False
    return (activity_date.month, activity_date.day) >= EVENT_START_MONTH_DAY


def normalize_gender(raw_gender):
    if not raw_gender:
        return None
    lowered = raw_gender.strip().lower()
    if lowered == "femme":
        return "Femme"
    if lowered == "homme":
        return "Homme"
    return None

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
                    try:
                        ele = float(child.text)
                    except (TypeError, ValueError):
                        ele = None
                elif child.tag.endswith("time"):
                    time_text = child.text
            points.append({"lat": lat, "lon": lon, "ele": ele, "time": time_text})

    if len(points) < 2:
        return {"distance_km": 0.0, "elevation_m": 0.0, "duration_min": 0.0, "created_at": None}

    total_distance = 0.0
    total_active_seconds = 0.0
    elevation_samples = []
    previous = None
    start_time = None
    for point in points:
        if point["ele"] is not None:
            elevation_samples.append(point["ele"])

        if previous is not None:
            total_distance += haversine(previous["lat"], previous["lon"], point["lat"], point["lon"])

            if previous["time"] and point["time"]:
                try:
                    prev_time = datetime.fromisoformat(previous["time"].replace("Z", "+00:00"))
                    cur_time = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
                except ValueError:
                    prev_time = cur_time = None
                if prev_time is not None and cur_time is not None:
                    if prev_time.tzinfo is not None:
                        prev_time = prev_time.astimezone(timezone.utc).replace(tzinfo=None)
                    if cur_time.tzinfo is not None:
                        cur_time = cur_time.astimezone(timezone.utc).replace(tzinfo=None)
                    delta = (cur_time - prev_time).total_seconds()
                    if delta > 0 and delta <= PAUSE_THRESHOLD_SECONDS:
                        total_active_seconds += delta

        if point["time"] and start_time is None:
            try:
                parsed_time = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
            except ValueError:
                parsed_time = None
            if parsed_time is not None:
                if parsed_time.tzinfo is not None:
                    parsed_time = parsed_time.astimezone(timezone.utc).replace(tzinfo=None)
                start_time = parsed_time

        previous = point

    duration_minutes = round(total_active_seconds / 60.0, 2)
    created_at = start_time.isoformat() if start_time else None
    total_elevation = calculate_ascent_hysteresis(elevation_samples)

    return {
        "distance_km": round(total_distance, 2),
        "elevation_m": round(total_elevation, 2),
        "duration_min": duration_minutes,
        "created_at": created_at,
    }


@app.route("/")
def index():
    user = get_current_user()
    conn = get_db()
    users = conn.execute("SELECT id, name, gender, profile_image FROM users ORDER BY name ASC").fetchall()

    selected_gender = normalize_gender(request.args.get("gender", ""))
    if selected_gender is None and user:
        selected_gender = normalize_gender(user["gender"])
    if selected_gender is None:
        selected_gender = "Homme"

    yellow_leaderboard = conn.execute(
        """
        SELECT
            id,
            name,
            gender,
            profile_image,
            total_distance_km,
            total_duration_min,
            CASE
                WHEN total_duration_min > 0 THEN ROUND((total_distance_km / total_duration_min) * 60.0, 2)
                ELSE 0
            END AS avg_speed_kmh
        FROM users
        WHERE gender = ?
        ORDER BY avg_speed_kmh DESC, total_distance_km DESC, name ASC
        """
    , (selected_gender,)).fetchall()
    white_leaderboard = conn.execute(
        "SELECT id, name, gender, profile_image, total_distance_km FROM users WHERE gender = ? ORDER BY total_distance_km DESC, name ASC",
        (selected_gender,),
    ).fetchall()
    elevation_leaderboard = conn.execute(
        "SELECT id, name, gender, profile_image, total_elevation_m FROM users WHERE gender = ? ORDER BY total_elevation_m DESC, name ASC",
        (selected_gender,),
    ).fetchall()
    duration_leaderboard = conn.execute(
        "SELECT id, name, gender, profile_image, total_duration_min FROM users WHERE gender = ? ORDER BY total_duration_min DESC, name ASC",
        (selected_gender,),
    ).fetchall()
    daily_winners_all = conn.execute(
        """
        WITH daily_totals AS (
            SELECT
                date(r.created_at) AS ride_day,
                r.user_id,
                u.gender,
                ROUND(SUM(r.distance_km), 2) AS distance_km,
                ROUND(SUM(r.duration_min), 2) AS duration_min,
                CASE
                    WHEN SUM(r.duration_min) > 0 THEN ROUND((SUM(r.distance_km) / SUM(r.duration_min)) * 60.0, 2)
                    ELSE 0
                END AS avg_speed_kmh
            FROM rides r
            JOIN users u ON r.user_id = u.id
            GROUP BY date(r.created_at), r.user_id, u.gender
        )
        SELECT
            strftime('%d.%m.%Y', dt.ride_day) AS day,
            dt.user_id,
            dt.gender,
            dt.distance_km,
            dt.duration_min,
            dt.avg_speed_kmh,
            u.name AS user_name,
            u.profile_image AS profile_image
        FROM daily_totals dt
        JOIN users u ON dt.user_id = u.id
        WHERE NOT EXISTS (
            SELECT 1
            FROM daily_totals better
            WHERE better.ride_day = dt.ride_day
                            AND better.gender = dt.gender
              AND (
                  better.avg_speed_kmh > dt.avg_speed_kmh
                  OR (
                      better.avg_speed_kmh = dt.avg_speed_kmh
                      AND better.distance_km > dt.distance_km
                  )
                  OR (
                      better.avg_speed_kmh = dt.avg_speed_kmh
                      AND better.distance_km = dt.distance_km
                      AND better.user_id < dt.user_id
                  )
              )
        )
        ORDER BY dt.ride_day DESC, dt.gender ASC
        """
    ).fetchall()
    daily_winners = [row for row in daily_winners_all if row["gender"] == selected_gender]

    daily_winner_homme = next((row for row in daily_winners_all if row["gender"] == "Homme"), None)
    daily_winner_femme = next((row for row in daily_winners_all if row["gender"] == "Femme"), None)
    rides = []
    if user:
        rides = conn.execute(
            "SELECT *, strftime('%d.%m.%Y', created_at) as ride_date FROM rides WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (user["id"],),
        ).fetchall()

    current_leader = None
    if yellow_leaderboard:
        current_leader = yellow_leaderboard[0]

    stats = {
        "distance": 0.0,
        "elevation": 0.0,
        "duration": 0.0,
    }
    if user:
        stats = {
            "distance": user["total_distance_km"],
            "elevation": user["total_elevation_m"],
            "duration": user["total_duration_min"],
        }

    return render_template(
        "index.html",
        user=user,
        users=users,
        selected_gender=selected_gender,
        genders=VALID_GENDERS,
        current_leader=current_leader,
        stats=stats,
        yellow_leaderboard=yellow_leaderboard,
        white_leaderboard=white_leaderboard,
        elevation_leaderboard=elevation_leaderboard,
        duration_leaderboard=duration_leaderboard,
        daily_winners=daily_winners,
        daily_winner_homme=daily_winner_homme,
        daily_winner_femme=daily_winner_femme,
        rides=rides,
    )


@app.route("/login", methods=["POST"])
def login():
    selected_name = request.form.get("existing_user", "").strip()
    selected_gender = normalize_gender(request.form.get("gender", ""))
    name = request.form.get("name", "").strip()
    if selected_name:
        name = selected_name
    if not name:
        return redirect(url_for("index"))

    conn = get_db()
    existing = conn.execute("SELECT id, gender, profile_image FROM users WHERE name = ?", (name,)).fetchone()

    profile_image = request.files.get("profile_image")
    filename = None
    if profile_image and profile_image.filename:
        if not allowed_image(profile_image.filename):
            return redirect(url_for("index"))
        filename = secure_filename(profile_image.filename)
        filename = f"{uuid.uuid4().hex}_{filename}"
        profile_image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    if existing:
        if not existing["profile_image"] and not filename:
            return redirect(url_for("index"))
        if filename and (existing["profile_image"] != filename):
            conn.execute("UPDATE users SET profile_image = ? WHERE id = ?", (filename, existing["id"]))
        if not existing["gender"] and selected_gender:
            conn.execute("UPDATE users SET gender = ? WHERE id = ?", (selected_gender, existing["id"]))
        user_id = existing["id"]
        user_gender = existing["gender"] or selected_gender or "Homme"
    else:
        if not filename:
            return redirect(url_for("index"))
        if selected_gender is None:
            return redirect(url_for("index"))
        cur = conn.execute(
            "INSERT INTO users (name, gender, profile_image, total_distance_km) VALUES (?, ?, ?, 0)",
            (name, selected_gender, filename),
        )
        user_id = cur.lastrowid
        user_gender = selected_gender

    conn.commit()
    session.permanent = True
    session["user_id"] = user_id
    session["user_name"] = name
    session["user_gender"] = user_gender
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/profile-image", methods=["POST"])
def update_profile_image():
    user = get_current_user()
    if not user:
        return redirect(url_for("index"))

    profile_image = request.files.get("profile_image")
    if not profile_image or not profile_image.filename:
        return redirect(url_for("index"))
    if not allowed_image(profile_image.filename):
        return redirect(url_for("index"))

    filename = secure_filename(profile_image.filename)
    filename = f"{uuid.uuid4().hex}_{filename}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    profile_image.save(filepath)

    conn = get_db()
    conn.execute("UPDATE users SET profile_image = ? WHERE id = ?", (filename, user["id"]))
    conn.commit()

    old_profile = user["profile_image"]
    if old_profile and old_profile != filename:
        old_path = os.path.join(app.config["UPLOAD_FOLDER"], old_profile)
        if os.path.exists(old_path):
            os.remove(old_path)

    return redirect(url_for("index"))


@app.route("/profile-gender", methods=["POST"])
def update_profile_gender():
    user = get_current_user()
    if not user:
        return redirect(url_for("index"))

    gender = normalize_gender(request.form.get("gender", ""))
    if gender is None:
        return redirect(url_for("index"))

    conn = get_db()
    conn.execute("UPDATE users SET gender = ? WHERE id = ?", (gender, user["id"]))
    conn.commit()
    session["user_gender"] = gender
    return redirect(url_for("index", gender=gender))


@app.route("/upload", methods=["POST"])
def upload():
    if not get_current_user():
        return redirect(url_for("index"))

    upload_files = request.files.getlist("gpx_file")
    valid_files = [f for f in upload_files if f and f.filename and allowed_gpx(f.filename)]
    if not valid_files:
        return redirect(url_for("index"))

    conn = get_db()
    total_distance = 0.0
    total_elevation = 0.0
    total_duration = 0.0

    for gpx_file in valid_files:
        filename = secure_filename(gpx_file.filename)
        filename = f"{uuid.uuid4().hex}_{filename}"
        gpx_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        gpx_file.save(gpx_path)

        try:
            metrics = parse_gpx_metrics(gpx_path)
        except Exception:
            if os.path.exists(gpx_path):
                os.remove(gpx_path)
            continue

        created_at = metrics["created_at"] or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        if not is_allowed_event_date(created_at):
            if os.path.exists(gpx_path):
                os.remove(gpx_path)
            continue

        conn.execute(
            "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                session["user_id"],
                filename,
                metrics["distance_km"],
                metrics["elevation_m"],
                metrics["duration_min"],
                created_at,
            ),
        )

        total_distance += metrics["distance_km"]
        total_elevation += metrics["elevation_m"]
        total_duration += metrics["duration_min"]

    if total_distance or total_elevation or total_duration:
        conn.execute(
            "UPDATE users SET total_distance_km = total_distance_km + ?, total_elevation_m = total_elevation_m + ?, total_duration_min = total_duration_min + ? WHERE id = ?",
            (
                round(total_distance, 2),
                round(total_elevation, 2),
                round(total_duration, 2),
                session["user_id"],
            ),
        )

    conn.commit()
    return redirect(url_for("index"))


@app.route("/rides/<int:ride_id>/delete", methods=["POST"])
def delete_ride(ride_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("index"))

    conn = get_db()
    ride = conn.execute(
        "SELECT id, user_id, filename FROM rides WHERE id = ? AND user_id = ?",
        (ride_id, user["id"]),
    ).fetchone()
    if not ride:
        return redirect(url_for("index"))

    conn.execute("DELETE FROM rides WHERE id = ?", (ride_id,))
    totals = conn.execute(
        """
        SELECT
            COALESCE(SUM(distance_km), 0) AS total_distance_km,
            COALESCE(SUM(elevation_m), 0) AS total_elevation_m,
            COALESCE(SUM(duration_min), 0) AS total_duration_min
        FROM rides
        WHERE user_id = ?
        """,
        (user["id"],),
    ).fetchone()
    conn.execute(
        "UPDATE users SET total_distance_km = ?, total_elevation_m = ?, total_duration_min = ? WHERE id = ?",
        (
            round(totals["total_distance_km"], 2),
            round(totals["total_elevation_m"], 2),
            round(totals["total_duration_min"], 2),
            user["id"],
        ),
    )
    conn.commit()

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], ride["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)

    return redirect(url_for("index"))


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
