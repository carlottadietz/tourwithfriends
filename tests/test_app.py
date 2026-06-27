import os
import sqlite3
import tempfile
import unittest
from io import BytesIO

from app import app, init_db, parse_gpx_metrics


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
