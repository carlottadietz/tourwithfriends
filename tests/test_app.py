import os
import tempfile
import unittest
from io import BytesIO

from app import app, init_db


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
        self.assertIn(b"Tour de France", response.data)

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
        self.assertIn(b"Leaderboard", response.data)


if __name__ == "__main__":
    unittest.main()
