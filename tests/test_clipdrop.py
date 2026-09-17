"""
Automated Test Suite for ClipDrop
Tests URL normalization, SSRF protection, filename sanitization,
health endpoints, API error responses, and static asset handling.
"""

import os
import sys
import unittest

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app

class ClipDropTestCase(unittest.TestCase):
    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()

    # -------------------------------------------------------------------------
    # URL Normalization & SSRF Defense
    # -------------------------------------------------------------------------

    def test_url_normalization_valid_formats(self):
        """Verifies that all standard YouTube link formats normalize to canonical watch URLs."""
        valid_cases = [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ?si=abc123xyz", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?feature=shared&v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
            ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ]
        for raw, expected_id in valid_cases:
            canonical, vid_id, err = app.normalize_youtube_url(raw)
            self.assertIsNone(err, f"Expected no error for {raw}")
            self.assertEqual(vid_id, expected_id, f"ID mismatch for {raw}")
            self.assertEqual(canonical, f"https://www.youtube.com/watch?v={expected_id}")

    def test_ssrf_and_malicious_urls(self):
        """Verifies that internal networks, localhost, metadata endpoints, and external domains are rejected."""
        malicious_cases = [
            ("http://localhost:5000", "SSRF_BLOCKED"),
            ("http://127.0.0.1:8080", "SSRF_BLOCKED"),
            ("http://169.254.169.254/latest/meta-data", "SSRF_BLOCKED"),
            ("http://0.0.0.0", "SSRF_BLOCKED"),
            ("file:///etc/passwd", "UNSUPPORTED_PROTOCOL"),
            ("https://google.com/search?q=video", "NOT_YOUTUBE_DOMAIN"),
            ("https://attacker-youtube.com/watch?v=dQw4w9WgXcQ", "NOT_YOUTUBE_DOMAIN"),
            ("https://youtube.com.attacker.com/watch?v=dQw4w9WgXcQ", "NOT_YOUTUBE_DOMAIN"),
            ("", "EMPTY_URL"),
            ("   ", "EMPTY_URL"),
            ("https://www.youtube.com/watch?v=short", "INVALID_YOUTUBE_URL"),
        ]
        for raw, expected_err in malicious_cases:
            canonical, vid_id, err = app.normalize_youtube_url(raw)
            self.assertIsNone(canonical, f"Should reject malicious input: {raw}")
            self.assertEqual(err, expected_err, f"Wrong error code for {raw}: got {err}")

    # -------------------------------------------------------------------------
    # Filename Sanitization & Path Traversal
    # -------------------------------------------------------------------------

    def test_filename_sanitization(self):
        """Tests that filenames are sanitized against path traversal and forbidden filesystem characters."""
        # Traversal attempt
        safe1 = app.sanitize_filename("../../../etc/passwd", "mp4")
        self.assertNotIn("/", safe1)
        self.assertNotIn("\\", safe1)
        self.assertTrue(safe1.endswith(".mp4"))

        # Windows forbidden characters: < > : " / \ | ? *
        safe2 = app.sanitize_filename('Video: "Awesome" *Test* <1080p>? Yes | No', "mp3")
        for char in '<>:"/\\|?*':
            self.assertNotIn(char, safe2)
        self.assertTrue(safe2.endswith(".mp3"))

        # Empty fallback
        safe3 = app.sanitize_filename("", "mp4")
        self.assertEqual(safe3, "ClipDrop_media.mp4")

    # -------------------------------------------------------------------------
    # Health Endpoint
    # -------------------------------------------------------------------------

    def test_health_endpoint(self):
        """Verifies that GET /health returns structured diagnostics."""
        res = self.client.get('/health')
        self.assertIn(res.status_code, [200, 503])
        data = res.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("status", data)
        self.assertIn("runtime", data)
        self.assertIn("yt_dlp", data)
        self.assertIn("yt_dlp_ejs", data)
        self.assertIn("deno", data)
        self.assertIn("ffmpeg", data)
        self.assertIn("tmp_writable", data)
        self.assertTrue(data["yt_dlp"])
        self.assertTrue(data["yt_dlp_ejs"])
        self.assertTrue(data["tmp_writable"])

    # -------------------------------------------------------------------------
    # API Routes Validation
    # -------------------------------------------------------------------------

    def test_info_endpoint_invalid_url(self):
        """POST /api/info with an invalid URL must return HTTP 400 with structured JSON."""
        res = self.client.post('/api/info', json={"url": "https://evil.com/fake"})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], "NOT_YOUTUBE_DOMAIN")

    def test_info_endpoint_empty_payload(self):
        """POST /api/info with empty JSON must return HTTP 400."""
        res = self.client.post('/api/info', json={})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "EMPTY_URL")

    def test_download_endpoint_invalid_type(self):
        """POST /api/download with an unsupported media type returns HTTP 400."""
        res = self.client.post('/api/download', json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "type": "invalid_type"
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "INVALID_TYPE")

    def test_index_route(self):
        """GET / returns the index.html page."""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"ClipDrop", res.data)

    def test_legacy_download_wrappers(self):
        """Verifies legacy download endpoints return graceful migration responses."""
        res_start = self.client.post('/api/download/start')
        self.assertEqual(res_start.status_code, 200)
        
        res_prog = self.client.get('/api/download/progress/test-job')
        self.assertEqual(res_prog.status_code, 200)
        
        res_file = self.client.get('/api/download/file/test-job')
        self.assertEqual(res_file.status_code, 400)

if __name__ == '__main__':
    unittest.main()
