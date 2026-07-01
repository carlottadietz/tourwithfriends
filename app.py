import os
import sqlite3
import uuid
import json
from datetime import datetime, timezone, timedelta
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, g, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "tour-with-friends-secret"),
    DATABASE_PATH=os.environ.get("DATABASE_PATH", str(Path(__file__).resolve().parent / "tourwithfriends.db")),
    UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER", str(Path(__file__).resolve().parent / "uploads")),
    STRAVA_CLIENT_ID=os.environ.get("STRAVA_CLIENT_ID", ""),
    STRAVA_CLIENT_SECRET=os.environ.get("STRAVA_CLIENT_SECRET", ""),
    STRAVA_REDIRECT_URI=os.environ.get("STRAVA_REDIRECT_URI", ""),
    SUPPORT_PUBLIC_URL=os.environ.get("SUPPORT_PUBLIC_URL", ""),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=40),
)

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS support_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            support_request_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(support_request_id) REFERENCES support_requests(id) ON DELETE CASCADE
        )
        """
    )
    columns = conn.execute("PRAGMA table_info(users)").fetchall()
    column_names = {col[1] for col in columns}
    if "gender" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN gender TEXT NOT NULL DEFAULT 'Homme'")
    if "strava_athlete_id" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN strava_athlete_id TEXT")
    if "strava_access_token" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN strava_access_token TEXT")
    if "strava_refresh_token" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN strava_refresh_token TEXT")
    if "strava_token_expires_at" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN strava_token_expires_at INTEGER DEFAULT 0")

    ride_columns = conn.execute("PRAGMA table_info(rides)").fetchall()
    ride_column_names = {col[1] for col in ride_columns}
    if "strava_activity_id" not in ride_column_names:
        conn.execute("ALTER TABLE rides ADD COLUMN strava_activity_id TEXT")
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
    current_time = app.config.get("CURRENT_TIME_OVERRIDE")
    if current_time is None:
        current_time = datetime.now()
    current_date = current_time.date()
    if (current_date.month, current_date.day) < EVENT_START_MONTH_DAY:
        return False
    return activity_date == current_date


def get_strava_activity_created_at(activity):
    local_start = normalize_iso_datetime(activity.get("start_date_local"))
    if local_start:
        return local_start
    return normalize_iso_datetime(activity.get("start_date"))


def recalculate_user_totals(conn, user_id):
    totals = conn.execute(
        """
        SELECT
            COALESCE(SUM(distance_km), 0) AS total_distance_km,
            COALESCE(SUM(elevation_m), 0) AS total_elevation_m,
            COALESCE(SUM(duration_min), 0) AS total_duration_min
        FROM rides
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    conn.execute(
        "UPDATE users SET total_distance_km = ?, total_elevation_m = ?, total_duration_min = ? WHERE id = ?",
        (
            round(totals["total_distance_km"], 2),
            round(totals["total_elevation_m"], 2),
            round(totals["total_duration_min"], 2),
            user_id,
        ),
    )


def normalize_gender(raw_gender):
    if not raw_gender:
        return None
    lowered = raw_gender.strip().lower()
    if lowered == "femme":
        return "Femme"
    if lowered == "homme":
        return "Homme"
    return None


def normalize_iso_datetime(raw_value):
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat()


def build_unique_username(conn, base_name):
    root_name = (base_name or "Strava Rider").strip() or "Strava Rider"
    candidate = root_name
    suffix = 2
    while conn.execute("SELECT 1 FROM users WHERE name = ?", (candidate,)).fetchone():
        candidate = f"{root_name} {suffix}"
        suffix += 1
    return candidate


def post_form_json(url, payload):
    body = urlencode(payload).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def get_json(url, headers=None, query=None):
    query = query or {}
    query_string = urlencode(query)
    full_url = f"{url}?{query_string}" if query_string else url
    req = Request(full_url, method="GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def refresh_strava_access_token(conn, user):
    refresh_token = user["strava_refresh_token"]
    if not refresh_token or not app.config["STRAVA_CLIENT_ID"] or not app.config["STRAVA_CLIENT_SECRET"]:
        return None

    refreshed = post_form_json(
        STRAVA_TOKEN_URL,
        {
            "client_id": app.config["STRAVA_CLIENT_ID"],
            "client_secret": app.config["STRAVA_CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if not refreshed or not refreshed.get("access_token"):
        return None

    conn.execute(
        """
        UPDATE users
        SET strava_access_token = ?, strava_refresh_token = ?, strava_token_expires_at = ?
        WHERE id = ?
        """,
        (
            refreshed.get("access_token"),
            refreshed.get("refresh_token", refresh_token),
            int(refreshed.get("expires_at", 0) or 0),
            user["id"],
        ),
    )
    conn.commit()
    return refreshed.get("access_token")


def get_valid_strava_token(conn, user):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    token = user["strava_access_token"]
    expires_at = int(user["strava_token_expires_at"] or 0)
    if token and expires_at - 60 > now_ts:
        return token
    return refresh_strava_access_token(conn, user)


def import_strava_activities_for_user(conn, user):
    access_token = get_valid_strava_token(conn, user)
    if not access_token:
        return 0

    current_time = app.config.get("CURRENT_TIME_OVERRIDE")
    if current_time is None:
        current_time = datetime.now()

    event_start_date = f"{current_time.year:04d}-{EVENT_START_MONTH_DAY[0]:02d}-{EVENT_START_MONTH_DAY[1]:02d}"
    conn.execute(
        "DELETE FROM rides WHERE user_id = ? AND strava_activity_id IS NOT NULL AND date(created_at) < ?",
        (user["id"], event_start_date),
    )
    recalculate_user_totals(conn, user["id"])

    imported_count = 0
    total_distance = 0.0
    total_elevation = 0.0
    total_duration = 0.0

    headers = {"Authorization": f"Bearer {access_token}"}
    today_start = datetime(current_time.year, current_time.month, current_time.day, tzinfo=timezone.utc)
    for page in range(1, 6):
        activities = get_json(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=headers,
            query={
                "per_page": 50,
                "page": page,
                "after": int(today_start.timestamp()),
            },
        )
        if not isinstance(activities, list) or not activities:
            break

        for activity in activities:
            activity_type = str(activity.get("type", ""))
            if activity_type not in {"Ride", "VirtualRide", "EBikeRide"}:
                continue

            strava_activity_id = str(activity.get("id", "")).strip()
            if not strava_activity_id:
                continue

            exists = conn.execute(
                "SELECT 1 FROM rides WHERE user_id = ? AND strava_activity_id = ?",
                (user["id"], strava_activity_id),
            ).fetchone()
            if exists:
                continue

            created_at = get_strava_activity_created_at(activity)
            if not created_at or not is_allowed_event_date(created_at):
                continue

            distance_km = round(float(activity.get("distance", 0.0)) / 1000.0, 2)
            elevation_m = round(float(activity.get("total_elevation_gain", 0.0)), 2)
            duration_min = round(float(activity.get("moving_time", 0.0)) / 60.0, 2)
            ride_name = (activity.get("name") or f"Strava Ride {strava_activity_id}").strip()
            filename = f"strava_{strava_activity_id}_{secure_filename(ride_name)}"

            conn.execute(
                """
                INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at, strava_activity_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], filename, distance_km, elevation_m, duration_min, created_at, strava_activity_id),
            )
            imported_count += 1
            total_distance += distance_km
            total_elevation += elevation_m
            total_duration += duration_min

        if len(activities) < 50:
            break

    if imported_count:
        conn.execute(
            """
            UPDATE users
            SET total_distance_km = total_distance_km + ?,
                total_elevation_m = total_elevation_m + ?,
                total_duration_min = total_duration_min + ?
            WHERE id = ?
            """,
            (round(total_distance, 2), round(total_elevation, 2), round(total_duration, 2), user["id"]),
        )
        conn.commit()

    return imported_count

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
    stage_days = []
    stage_days_map = {}
    for row in daily_winners_all:
        day = row["day"]
        if day not in stage_days_map:
            stage_days_map[day] = {"day": day, "Femme": None, "Homme": None}
            stage_days.append(stage_days_map[day])
        stage_days_map[day][row["gender"]] = row

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

    strava_connected = bool(user and user["strava_refresh_token"])
    strava_enabled = bool(app.config["STRAVA_CLIENT_ID"] and app.config["STRAVA_CLIENT_SECRET"])
    strava_imported_count = session.pop("strava_imported_count", None)
    support_public_url = app.config["SUPPORT_PUBLIC_URL"] or url_for("support", _external=True)

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
        stage_days=stage_days,
        rides=rides,
        strava_enabled=strava_enabled,
        strava_connected=strava_connected,
        strava_imported_count=strava_imported_count,
        support_public_url=support_public_url,
    )


@app.route("/support", methods=["GET", "POST"])
def support():
    user = get_current_user()
    status = request.args.get("status", "")
    error = None
    conn = get_db()
    form_data = {
        "name": user["name"] if user else "",
        "email": "",
        "issue_type": "Support",
        "message": "",
    }
    comment_defaults = {
        "name": user["name"] if user else "",
    }

    if request.method == "POST":
        action = request.form.get("action", "create_request")
        if action == "add_comment":
            comment_name = request.form.get("comment_name", "").strip()
            comment_text = request.form.get("comment", "").strip()
            request_id_raw = request.form.get("support_request_id", "").strip()
            try:
                request_id = int(request_id_raw)
            except ValueError:
                request_id = 0

            existing_request = conn.execute(
                "SELECT id FROM support_requests WHERE id = ?",
                (request_id,),
            ).fetchone()

            if not comment_name or not comment_text:
                error = "Bitte gib einen Namen und einen Kommentar ein."
            elif not existing_request:
                error = "Die ausgewählte Support-Anfrage existiert nicht mehr."
            else:
                conn.execute(
                    "INSERT INTO support_comments (support_request_id, name, comment, created_at) VALUES (?, ?, ?, ?)",
                    (request_id, comment_name, comment_text, datetime.utcnow().isoformat()),
                )
                conn.commit()
                return redirect(url_for("support", status="commented"))
        else:
            form_data = {
                "name": request.form.get("name", "").strip(),
                "email": request.form.get("email", "").strip(),
                "issue_type": request.form.get("issue_type", "Support").strip() or "Support",
                "message": request.form.get("message", "").strip(),
            }

            if not all(form_data.values()):
                error = "Bitte fülle alle Felder aus."
            elif "@" not in form_data["email"]:
                error = "Bitte gib eine gültige E-Mail-Adresse an."
            else:
                conn.execute(
                    "INSERT INTO support_requests (name, email, issue_type, message, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        form_data["name"],
                        form_data["email"],
                        form_data["issue_type"],
                        form_data["message"],
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
                return redirect(url_for("support", status="created"))

    support_requests = conn.execute(
        """
        SELECT
            sr.id,
            sr.name,
            sr.email,
            sr.issue_type,
            sr.message,
            strftime('%d.%m.%Y %H:%M', sr.created_at) AS created_at_label,
            COUNT(sc.id) AS comment_count
        FROM support_requests sr
        LEFT JOIN support_comments sc ON sc.support_request_id = sr.id
        GROUP BY sr.id
        ORDER BY sr.created_at DESC, sr.id DESC
        """
    ).fetchall()
    comments = conn.execute(
        """
        SELECT
            id,
            support_request_id,
            name,
            comment,
            strftime('%d.%m.%Y %H:%M', created_at) AS created_at_label
        FROM support_comments
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    comments_by_request = {}
    for comment in comments:
        comments_by_request.setdefault(comment["support_request_id"], []).append(comment)

    support_public_url = app.config["SUPPORT_PUBLIC_URL"] or url_for("support", _external=True)
    return render_template(
        "support.html",
        user=user,
        error=error,
        status=status,
        form_data=form_data,
        comment_defaults=comment_defaults,
        support_requests=support_requests,
        comments_by_request=comments_by_request,
        support_public_url=support_public_url,
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


@app.route("/strava/login")
def strava_login():
    if not app.config["STRAVA_CLIENT_ID"] or not app.config["STRAVA_CLIENT_SECRET"]:
        return redirect(url_for("index"))

    current_user = get_current_user()
    if current_user:
        session["strava_link_user_id"] = current_user["id"]
    else:
        session.pop("strava_link_user_id", None)

    state = uuid.uuid4().hex
    session["strava_oauth_state"] = state
    redirect_uri = app.config["STRAVA_REDIRECT_URI"] or url_for("strava_callback", _external=True)
    params = {
        "client_id": app.config["STRAVA_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        "state": state,
    }
    return redirect(f"{STRAVA_AUTH_URL}?{urlencode(params)}")


@app.route("/strava/callback")
def strava_callback():
    expected_state = session.pop("strava_oauth_state", "")
    if not expected_state or request.args.get("state") != expected_state:
        return redirect(url_for("index"))

    code = request.args.get("code", "")
    if not code:
        return redirect(url_for("index"))

    redirect_uri = app.config["STRAVA_REDIRECT_URI"] or url_for("strava_callback", _external=True)
    token_data = post_form_json(
        STRAVA_TOKEN_URL,
        {
            "client_id": app.config["STRAVA_CLIENT_ID"],
            "client_secret": app.config["STRAVA_CLIENT_SECRET"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if not token_data or not token_data.get("access_token"):
        return redirect(url_for("index"))

    athlete = token_data.get("athlete") or {}
    athlete_id = str(athlete.get("id", "")).strip()
    if not athlete_id:
        athlete_profile = get_json(
            f"{STRAVA_API_BASE}/athlete",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        athlete = athlete_profile if isinstance(athlete_profile, dict) else {}
        athlete_id = str(athlete.get("id", "")).strip()
    if not athlete_id:
        return redirect(url_for("index"))

    first_name = (athlete.get("firstname") or "").strip()
    last_name = (athlete.get("lastname") or "").strip()
    base_name = f"{first_name} {last_name}".strip() or f"Strava {athlete_id}"

    conn = get_db()
    link_user_id = session.pop("strava_link_user_id", None)
    existing_owner = conn.execute("SELECT * FROM users WHERE strava_athlete_id = ?", (athlete_id,)).fetchone()

    if link_user_id:
        linked_user = conn.execute("SELECT * FROM users WHERE id = ?", (link_user_id,)).fetchone()
        if not linked_user:
            return redirect(url_for("index"))
        if existing_owner and existing_owner["id"] != linked_user["id"]:
            return redirect(url_for("index"))

        conn.execute(
            """
            UPDATE users
            SET strava_athlete_id = ?,
                strava_access_token = ?,
                strava_refresh_token = ?,
                strava_token_expires_at = ?
            WHERE id = ?
            """,
            (
                athlete_id,
                token_data.get("access_token"),
                token_data.get("refresh_token"),
                int(token_data.get("expires_at", 0) or 0),
                linked_user["id"],
            ),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (linked_user["id"],)).fetchone()
    elif existing_owner:
        conn.execute(
            """
            UPDATE users
            SET strava_access_token = ?,
                strava_refresh_token = ?,
                strava_token_expires_at = ?
            WHERE id = ?
            """,
            (
                token_data.get("access_token"),
                token_data.get("refresh_token"),
                int(token_data.get("expires_at", 0) or 0),
                existing_owner["id"],
            ),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (existing_owner["id"],)).fetchone()
    else:
        name = build_unique_username(conn, base_name)
        cur = conn.execute(
            """
            INSERT INTO users (
                name,
                gender,
                profile_image,
                total_distance_km,
                total_elevation_m,
                total_duration_min,
                strava_athlete_id,
                strava_access_token,
                strava_refresh_token,
                strava_token_expires_at
            )
            VALUES (?, 'Homme', NULL, 0, 0, 0, ?, ?, ?, ?)
            """,
            (
                name,
                athlete_id,
                token_data.get("access_token"),
                token_data.get("refresh_token"),
                int(token_data.get("expires_at", 0) or 0),
            ),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

    imported_count = import_strava_activities_for_user(conn, user)
    session["strava_imported_count"] = imported_count
    session.permanent = True
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["user_gender"] = user["gender"]
    return redirect(url_for("index"))


@app.route("/strava/import", methods=["POST"])
def strava_import():
    user = get_current_user()
    if not user:
        return redirect(url_for("index"))
    if not user["strava_refresh_token"]:
        return redirect(url_for("strava_login"))

    conn = get_db()
    imported_count = import_strava_activities_for_user(conn, user)
    session["strava_imported_count"] = imported_count
    return redirect(url_for("index"))


@app.route("/strava/disconnect", methods=["POST"])
def strava_disconnect():
    user = get_current_user()
    if not user:
        return redirect(url_for("index"))

    conn = get_db()
    conn.execute(
        """
        UPDATE users
        SET strava_athlete_id = NULL,
            strava_access_token = NULL,
            strava_refresh_token = NULL,
            strava_token_expires_at = 0
        WHERE id = ?
        """,
        (user["id"],),
    )
    conn.commit()
    session.pop("strava_imported_count", None)
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
    recalculate_user_totals(conn, user["id"])
    conn.commit()

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], ride["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)

    return redirect(url_for("index"))


@app.route("/rides/delete-all", methods=["POST"])
def delete_all_rides():
    user = get_current_user()
    if not user:
        return redirect(url_for("index"))

    conn = get_db()
    rides = conn.execute(
        "SELECT filename FROM rides WHERE user_id = ?",
        (user["id"],),
    ).fetchall()
    if not rides:
        return redirect(url_for("index"))

    conn.execute("DELETE FROM rides WHERE user_id = ?", (user["id"],))
    recalculate_user_totals(conn, user["id"])
    conn.commit()

    for ride in rides:
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], ride["filename"])
        if os.path.exists(filepath):
            os.remove(filepath)

    return redirect(url_for("index"))


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
