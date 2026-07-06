import os
import sqlite3
import tempfile
import unittest
from io import BytesIO

from unittest.mock import patch
from datetime import datetime

from app import app, get_db, import_strava_activities_for_user, init_db, parse_gpx_metrics, run_gpx_recalculation_migration


class TourWithFriendsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.db")
        self.upload_dir = os.path.join(self.tmp_dir.name, "uploads")

        app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            DATABASE_PATH=self.db_path,
            UPLOAD_FOLDER=self.upload_dir,
            CURRENT_TIME_OVERRIDE=None,
        )

        with app.app_context():
            init_db()

        self.client = app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_home_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"tour with friends", response.data)

    def test_login_creates_user_and_shows_leaderboard(self):
        profile_image = (BytesIO(b"fake-image-data"), "avatar.png")
        response = self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": profile_image},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Anna", response.data)
        self.assertIn(b"Trikotwertung", response.data)

    def test_existing_user_can_log_in_again(self):
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        response = self.client.post(
            "/login",
            data={"existing_user": "Anna"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Hallo, Anna", response.data)

    def test_logged_in_user_can_log_out_to_login_screen(self):
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        response = self.client.post("/logout", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Bitte zuerst anmelden", response.data)

    def test_user_can_disconnect_strava_connection(self):
        app.config.update(
            STRAVA_CLIENT_ID="client-id",
            STRAVA_CLIENT_SECRET="client-secret",
        )
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        with self.client.session_transaction() as session_data:
            user_id = session_data["user_id"]

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE users SET strava_athlete_id = ?, strava_access_token = ?, strava_refresh_token = ?, strava_token_expires_at = ? WHERE id = ?",
            ("12345", "access-token", "refresh-token", 9999999999, user_id),
        )
        conn.commit()
        conn.close()

        response = self.client.post("/strava/disconnect", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Strava verbinden", response.data)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT strava_athlete_id, strava_access_token, strava_refresh_token, strava_token_expires_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        conn.close()

        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertEqual(row[3], 0)

    def test_support_page_renders(self):
        response = self.client.get("/support")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Support f\xc3\xbcr Tour with Friends", response.data)
        self.assertIn(b"Gesammelte Support-Anfragen", response.data)

    def test_support_form_creates_request(self):
        response = self.client.post(
            "/support",
            data={
                "name": "Lotti",
                "email": "lotti@example.com",
                "issue_type": "Bug",
                "message": "Beim Strava Login kommt ein Fehler.",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Support-Anfrage wurde gespeichert", response.data)
        self.assertIn(b"Beim Strava Login kommt ein Fehler.", response.data)

    def test_support_request_can_be_commented(self):
        self.client.post(
            "/support",
            data={
                "action": "create_request",
                "name": "Lotti",
                "email": "lotti@example.com",
                "issue_type": "Bug",
                "message": "Beim Strava Login kommt ein Fehler.",
            },
            follow_redirects=True,
        )

        conn = sqlite3.connect(self.db_path)
        request_id = conn.execute("SELECT id FROM support_requests").fetchone()[0]
        conn.close()

        response = self.client.post(
            "/support",
            data={
                "action": "add_comment",
                "support_request_id": str(request_id),
                "comment_name": "Carlotta",
                "comment": "Ich schaue es mir an.",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Kommentar wurde gespeichert", response.data)
        self.assertIn(b"Ich schaue es mir an.", response.data)

    @patch("app.get_valid_strava_token", return_value="token")
    @patch("app.get_json")
    def test_strava_import_uses_only_activities_from_04_07_onward(self, get_json_mock, _token_mock):
        app.config["CURRENT_TIME_OVERRIDE"] = datetime(2026, 7, 4, 12, 0, 0)
        get_json_mock.side_effect = [
            [
                {
                    "id": 1001,
                    "type": "Ride",
                    "distance": 15000,
                    "total_elevation_gain": 180,
                    "moving_time": 2400,
                    "name": "03.07 Ride",
                    "start_date": "2026-07-03T23:30:00Z",
                    "start_date_local": "2026-07-03T23:30:00",
                },
                {
                    "id": 1002,
                    "type": "Ride",
                    "distance": 22000,
                    "total_elevation_gain": 240,
                    "moving_time": 3600,
                    "name": "04.07 Ride",
                    "start_date": "2026-07-04T07:00:00Z",
                    "start_date_local": "2026-07-04T09:00:00",
                },
            ],
            [],
        ]

        with app.app_context():
            conn = get_db()
            conn.execute(
                "INSERT INTO users (name, gender, profile_image, total_distance_km) VALUES (?, ?, ?, 0)",
                ("Strava Tester", "Homme", "avatar.png"),
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE name = ?", ("Strava Tester",)).fetchone()

            imported_count = import_strava_activities_for_user(conn, user)

            rides = conn.execute("SELECT strava_activity_id, created_at FROM rides ORDER BY id ASC").fetchall()

        self.assertEqual(imported_count, 1)
        self.assertEqual(len(rides), 1)
        self.assertEqual(rides[0]["strava_activity_id"], "1002")
        self.assertTrue(rides[0]["created_at"].startswith("2026-07-04"))
        self.assertEqual(get_json_mock.call_args_list[0].kwargs["query"]["after"], 1783123200)

    def test_upload_rejects_previous_day_activity(self):
        app.config["CURRENT_TIME_OVERRIDE"] = datetime(2026, 7, 5, 10, 0, 0)
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        gpx_content = b"""<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
          <trk>
            <trkseg>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
              <trkpt lat='48.8576' lon='2.3532'><ele>120</ele><time>2026-07-04T10:10:00Z</time></trkpt>
            </trkseg>
          </trk>
        </gpx>"""

        response = self.client.post(
            "/upload",
            data={"gpx_file": (BytesIO(gpx_content), "ride_0407.gpx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Lade deine erste GPX-Datei hoch", response.data)

        conn = sqlite3.connect(self.db_path)
        ride_count = conn.execute("SELECT COUNT(*) FROM rides").fetchone()[0]
        conn.close()
        self.assertEqual(ride_count, 0)

    def test_upload_accepts_same_day_activity(self):
        app.config["CURRENT_TIME_OVERRIDE"] = datetime(2026, 7, 5, 10, 0, 0)
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        gpx_content = b"""<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
          <trk>
            <trkseg>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-05T10:00:00Z</time></trkpt>
              <trkpt lat='48.8576' lon='2.3532'><ele>120</ele><time>2026-07-05T10:10:00Z</time></trkpt>
            </trkseg>
          </trk>
        </gpx>"""

        response = self.client.post(
            "/upload",
            data={"gpx_file": (BytesIO(gpx_content), "ride_0507.gpx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ride_0507.gpx", response.data)

    @patch("app.parse_fit_metrics")
    def test_upload_accepts_same_day_fit_activity(self, parse_fit_metrics_mock):
        app.config["CURRENT_TIME_OVERRIDE"] = datetime(2026, 7, 5, 10, 0, 0)
        parse_fit_metrics_mock.return_value = {
            "distance_km": 25.5,
            "elevation_m": 117.0,
            "duration_min": 67.0,
            "created_at": "2026-07-05T09:00:00",
        }

        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        response = self.client.post(
            "/upload",
            data={"gpx_file": (BytesIO(b"fit-binary"), "ride.fit")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ride.fit", response.data)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT distance_km, elevation_m, duration_min FROM rides ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertAlmostEqual(row[0], 25.5, places=2)
        self.assertAlmostEqual(row[1], 117.0, places=2)
        self.assertAlmostEqual(row[2], 67.0, places=2)

    def test_manual_ride_can_be_saved_and_rendered_with_today_date(self):
        app.config["CURRENT_TIME_OVERRIDE"] = datetime(2026, 7, 5, 10, 0, 0)
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        response = self.client.post(
            "/rides/manual",
            data={"distance_km": "42.5", "avg_speed_kmh": "28.4", "duration_min": "89.8"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Heutiges Datum: 05.07.2026", response.data)
        self.assertIn(b"Manuelle Fahrt", response.data)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT distance_km, avg_speed_kmh, duration_min, created_at FROM rides ORDER BY id ASC"
        ).fetchall()
        totals = conn.execute(
            "SELECT total_distance_km, total_duration_min FROM users WHERE name = ?",
            ("Anna",),
        ).fetchone()
        conn.close()

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["distance_km"], 42.5, places=2)
        self.assertAlmostEqual(rows[0]["avg_speed_kmh"], 28.4, places=2)
        self.assertAlmostEqual(rows[0]["duration_min"], 89.8, places=2)
        self.assertTrue(rows[0]["created_at"].startswith("2026-07-05"))
        self.assertAlmostEqual(totals[0], 42.5, places=2)
        self.assertAlmostEqual(totals[1], 89.8, places=2)

    def test_manual_ride_allows_multiple_entries_on_same_day(self):
        app.config["CURRENT_TIME_OVERRIDE"] = datetime(2026, 7, 5, 10, 0, 0)
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.client.post(
            "/rides/manual",
            data={"distance_km": "20.0", "avg_speed_kmh": "25.0", "duration_min": "48.0"},
            follow_redirects=True,
        )
        self.client.post(
            "/rides/manual",
            data={"distance_km": "35.0", "avg_speed_kmh": "27.5", "duration_min": "76.4"},
            follow_redirects=True,
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT distance_km, avg_speed_kmh, duration_min FROM rides ORDER BY id ASC"
        ).fetchall()
        totals = conn.execute(
            "SELECT total_distance_km, total_duration_min FROM users WHERE name = ?",
            ("Anna",),
        ).fetchone()
        conn.close()

        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["distance_km"], 20.0, places=2)
        self.assertAlmostEqual(rows[1]["distance_km"], 35.0, places=2)
        self.assertAlmostEqual(totals[0], 55.0, places=2)
        self.assertAlmostEqual(totals[1], 124.4, places=2)

    @patch("app.get_valid_strava_token", return_value="token")
    @patch("app.get_json", return_value=[])
    def test_strava_import_removes_existing_pre_event_strava_rides(self, _get_json_mock, _token_mock):
        with app.app_context():
            conn = get_db()
            conn.execute(
                "INSERT INTO users (name, gender, profile_image, total_distance_km, total_elevation_m, total_duration_min) VALUES (?, ?, ?, ?, ?, ?)",
                ("Cleanup Tester", "Homme", "avatar.png", 50.0, 500.0, 120.0),
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE name = ?", ("Cleanup Tester",)).fetchone()
            conn.execute(
                "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at, strava_activity_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user["id"], "old_strava.gpx", 50.0, 500.0, 120.0, "2026-07-03T08:00:00", "legacy-1"),
            )
            conn.commit()

            imported_count = import_strava_activities_for_user(conn, user)

            remaining_rides = conn.execute("SELECT COUNT(*) AS count FROM rides WHERE user_id = ?", (user["id"],)).fetchone()["count"]
            updated_user = conn.execute(
                "SELECT total_distance_km, total_elevation_m, total_duration_min FROM users WHERE id = ?",
                (user["id"],),
            ).fetchone()

        self.assertEqual(imported_count, 0)
        self.assertEqual(remaining_rides, 0)
        self.assertEqual(updated_user["total_distance_km"], 0)
        self.assertEqual(updated_user["total_elevation_m"], 0)
        self.assertEqual(updated_user["total_duration_min"], 0)

    def test_parse_gpx_metrics_reads_distance_elevation_and_duration(self):
        gpx_content = """<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
          <trk>
            <trkseg>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
              <trkpt lat='48.8576' lon='2.3532'><ele>120</ele><time>2026-07-04T10:10:00Z</time></trkpt>
            </trkseg>
          </trk>
        </gpx>"""
        path = os.path.join(self.tmp_dir.name, "ride.gpx")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(gpx_content)

        metrics = parse_gpx_metrics(path)
        self.assertAlmostEqual(metrics["distance_km"], 0.13, places=2)
        self.assertAlmostEqual(metrics["elevation_m"], 20.0, places=2)
        self.assertAlmostEqual(metrics["duration_min"], 10.0, places=1)

    def test_parse_gpx_metrics_excludes_long_pauses_from_duration(self):
        gpx_content = """<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
          <trk>
            <trkseg>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
              <trkpt lat='48.8576' lon='2.3532'><ele>120</ele><time>2026-07-04T10:01:00Z</time></trkpt>
              <trkpt lat='48.8577' lon='2.3533'><ele>130</ele><time>2026-07-04T10:50:00Z</time></trkpt>
              <trkpt lat='48.8578' lon='2.3534'><ele>140</ele><time>2026-07-04T10:51:00Z</time></trkpt>
            </trkseg>
          </trk>
        </gpx>"""
        path = os.path.join(self.tmp_dir.name, "ride_pause.gpx")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(gpx_content)

        metrics = parse_gpx_metrics(path)
        self.assertTrue(metrics["distance_km"] > 0)
        self.assertAlmostEqual(metrics["duration_min"], 2.0, places=1)
        self.assertTrue(metrics["created_at"].startswith("2026-07-04T10:00:00"))

    def test_parse_gpx_metrics_excludes_stationary_stop_time(self):
        gpx_content = """<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
          <trk>
            <trkseg>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:10:00Z</time></trkpt>
              <trkpt lat='48.8576' lon='2.3532'><ele>120</ele><time>2026-07-04T10:20:00Z</time></trkpt>
            </trkseg>
          </trk>
        </gpx>"""
        path = os.path.join(self.tmp_dir.name, "ride_stationary_pause.gpx")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(gpx_content)

        metrics = parse_gpx_metrics(path)
        self.assertTrue(metrics["distance_km"] > 0)
        self.assertAlmostEqual(metrics["duration_min"], 10.0, places=1)

    def test_gpx_recalculation_migration_updates_existing_uploaded_rides(self):
        os.makedirs(self.upload_dir, exist_ok=True)
        gpx_name = "legacy_ride.gpx"
        gpx_path = os.path.join(self.upload_dir, gpx_name)
        gpx_content = """<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
          <trk>
            <trkseg>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
              <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:10:00Z</time></trkpt>
              <trkpt lat='48.8576' lon='2.3532'><ele>120</ele><time>2026-07-04T10:20:00Z</time></trkpt>
            </trkseg>
          </trk>
        </gpx>"""
        with open(gpx_path, "w", encoding="utf-8") as handle:
            handle.write(gpx_content)

        conn = sqlite3.connect(self.db_path)
        user_id = conn.execute(
            "INSERT INTO users (name, gender, profile_image, total_distance_km, total_elevation_m, total_duration_min) VALUES (?, ?, ?, ?, ?, ?)",
            ("Legacy User", "Homme", "avatar.png", 50.0, 500.0, 90.0),
        ).lastrowid
        ride_id = conn.execute(
            "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, gpx_name, 50.0, 500.0, 90.0, "2026-07-04T10:00:00"),
        ).lastrowid
        conn.commit()
        conn.close()

        updated_count = run_gpx_recalculation_migration()
        updated_count_second = run_gpx_recalculation_migration()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ride = conn.execute(
            "SELECT distance_km, elevation_m, duration_min FROM rides WHERE id = ?",
            (ride_id,),
        ).fetchone()
        user = conn.execute(
            "SELECT total_distance_km, total_elevation_m, total_duration_min FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        marker = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            ("gpx_metrics_recalculated_v2",),
        ).fetchone()
        conn.close()

        self.assertEqual(updated_count, 1)
        self.assertEqual(updated_count_second, 0)
        self.assertTrue(ride["distance_km"] > 0)
        self.assertAlmostEqual(ride["elevation_m"], 20.0, places=2)
        self.assertAlmostEqual(ride["duration_min"], 10.0, places=1)
        self.assertAlmostEqual(user["total_distance_km"], ride["distance_km"], places=2)
        self.assertAlmostEqual(user["total_elevation_m"], ride["elevation_m"], places=2)
        self.assertAlmostEqual(user["total_duration_min"], ride["duration_min"], places=2)
        self.assertIsNotNone(marker)

    def test_parse_gpx_metrics_ignores_missing_elevation_points(self):
        gpx_content = """<?xml version='1.0' encoding='UTF-8'?>
        <gpx version='1.1' creator='test'>
            <trk>
                <trkseg>
                    <trkpt lat='48.8566' lon='2.3522'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
                    <trkpt lat='48.8570' lon='2.3528'><time>2026-07-04T10:03:00Z</time></trkpt>
                    <trkpt lat='48.8576' lon='2.3532'><ele>105</ele><time>2026-07-04T10:06:00Z</time></trkpt>
                </trkseg>
            </trk>
        </gpx>"""
        path = os.path.join(self.tmp_dir.name, "ride_missing_ele.gpx")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(gpx_content)

        metrics = parse_gpx_metrics(path)
        self.assertTrue(metrics["distance_km"] > 0)
        self.assertEqual(metrics["elevation_m"], 0.0)

        def test_parse_gpx_metrics_applies_komoot_wahoo_elevation_uplift(self):
                gpx_content = """<?xml version='1.0' encoding='UTF-8'?>
                <gpx version='1.1' creator='https://www.komoot.de'>
                    <trk>
                        <trkseg>
                            <trkpt lat='46.0000' lon='11.0000'><ele>100</ele><time>2026-07-04T10:00:00Z</time></trkpt>
                            <trkpt lat='46.0001' lon='11.0001'><ele>105</ele><time>2026-07-04T10:01:00Z</time></trkpt>
                            <trkpt lat='46.0002' lon='11.0002'><ele>100</ele><time>2026-07-04T10:02:00Z</time></trkpt>
                            <trkpt lat='46.0003' lon='11.0003'><ele>105</ele><time>2026-07-04T10:03:00Z</time></trkpt>
                            <trkpt lat='46.0004' lon='11.0004'><ele>100</ele><time>2026-07-04T10:04:00Z</time></trkpt>
                            <trkpt lat='46.0005' lon='11.0005'><ele>105</ele><time>2026-07-04T10:05:00Z</time></trkpt>
                        </trkseg>
                    </trk>
                </gpx>"""
                path = os.path.join(self.tmp_dir.name, "ride_komoot_like.gpx")
                with open(path, "w", encoding="utf-8") as handle:
                        handle.write(gpx_content)

                metrics = parse_gpx_metrics(path)
                self.assertTrue(metrics["distance_km"] > 0)
                self.assertAlmostEqual(metrics["elevation_m"], 16.85, places=2)

    def test_user_can_delete_own_ride(self):
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        conn = sqlite3.connect(self.db_path)
        user_id = conn.execute("SELECT id FROM users WHERE name = ?", ("Anna",)).fetchone()[0]
        conn.execute(
            "UPDATE users SET total_distance_km = ?, total_elevation_m = ?, total_duration_min = ? WHERE id = ?",
            (25.5, 420.0, 88.0, user_id),
        )
        cur = conn.execute(
            "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "ride_test.gpx", 25.5, 420.0, 88.0, "2026-07-05T08:00:00"),
        )
        ride_id = cur.lastrowid
        conn.commit()

        response = self.client.post(f"/rides/{ride_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        remaining = conn.execute("SELECT COUNT(*) FROM rides WHERE id = ?", (ride_id,)).fetchone()[0]
        self.assertEqual(remaining, 0)
        totals = conn.execute(
            "SELECT total_distance_km, total_elevation_m, total_duration_min FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        self.assertEqual(totals[0], 0)
        self.assertEqual(totals[1], 0)
        self.assertEqual(totals[2], 0)
        conn.close()

    def test_user_can_delete_all_own_rides(self):
        self.client.post(
            "/login",
            data={"name": "Anna", "gender": "Femme", "profile_image": (BytesIO(b"fake-image-data"), "avatar.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        conn = sqlite3.connect(self.db_path)
        user_id = conn.execute("SELECT id FROM users WHERE name = ?", ("Anna",)).fetchone()[0]
        conn.execute(
            "UPDATE users SET total_distance_km = ?, total_elevation_m = ?, total_duration_min = ? WHERE id = ?",
            (60.0, 900.0, 150.0, user_id),
        )
        conn.execute(
            "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "ride_a.gpx", 25.0, 300.0, 60.0, "2026-07-05T08:00:00"),
        )
        conn.execute(
            "INSERT INTO rides (user_id, filename, distance_km, elevation_m, duration_min, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "ride_b.gpx", 35.0, 600.0, 90.0, "2026-07-06T09:00:00"),
        )
        conn.commit()
        conn.close()

        response = self.client.post("/rides/delete-all", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Lade deine erste GPX-Datei hoch", response.data)

        conn = sqlite3.connect(self.db_path)
        remaining = conn.execute("SELECT COUNT(*) FROM rides WHERE user_id = ?", (user_id,)).fetchone()[0]
        self.assertEqual(remaining, 0)
        totals = conn.execute(
            "SELECT total_distance_km, total_elevation_m, total_duration_min FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        self.assertEqual(totals[0], 0)
        self.assertEqual(totals[1], 0)
        self.assertEqual(totals[2], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
