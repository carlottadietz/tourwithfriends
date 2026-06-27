import os
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
        self.assertIn(b"Tour mit Freunden", response.data)

    def test_login_creates_user_and_shows_leaderboard(self):
        profile_image = (BytesIO(b"fake-image-data"), "avatar.png")
        response = self.client.post(
            "/login",
            data={"name": "Anna", "profile_image": profile_image},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Anna", response.data)
        self.assertIn(b"Trikotwertung", response.data)

    def test_existing_user_can_log_in_again(self):
        self.client.post(
            "/login",
            data={"name": "Anna"},
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
        self.assertGreater(metrics["distance_km"], 0)
        self.assertGreater(metrics["elevation_m"], 0)
        self.assertGreater(metrics["duration_min"], 0)


if __name__ == "__main__":
    unittest.main()
