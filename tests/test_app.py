import os
import sqlite3
import tempfile
import unittest
from io import BytesIO

from unittest.mock import patch

from app import app, get_db, import_strava_activities_for_user, init_db, parse_gpx_metrics


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


if __name__ == "__main__":
    unittest.main()
