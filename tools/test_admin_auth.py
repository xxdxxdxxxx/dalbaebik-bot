from __future__ import annotations
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import player_store
from tools import admin_ui

PUBLIC = "https://panel.example.com"


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        admin_ui._fails.clear()
        admin_ui._secret_cache = b"\x01" * 32  # не трогаем .admin_secret на диске
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY", "ADMIN_SESSION_DAYS"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        self.db_patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.db_patch.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()
        self.env.stop()
        admin_ui._secret_cache = None

    def login(self, password="s3cret-pass", **kw):
        return self.client.post("/login", data={"password": password}, base_url=PUBLIC, **kw)

    # --- без пароля: только localhost -------------------------------------
    def test_without_password_localhost_is_open_but_public_host_is_blocked(self):
        self.assertNotEqual(self.client.get("/", base_url="http://localhost:8787").status_code, 403)
        self.assertNotEqual(self.client.get("/", base_url="http://127.0.0.1:8123").status_code, 403)
        self.assertEqual(self.client.get("/", base_url=PUBLIC).status_code, 403)

    def test_without_password_forwarded_request_is_blocked(self):
        r = self.client.get("/", base_url="http://localhost:8787", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(r.status_code, 403)

    # --- с паролем ---------------------------------------------------------
    def test_requires_login_and_next_is_kept(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        r = self.client.get("/players", base_url=PUBLIC)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login?next=/players", r.headers["Location"])

    def test_wrong_password_rejected_right_password_sets_secure_cookie(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        os.environ["ADMIN_TRUST_PROXY"] = "1"
        with patch.object(admin_ui.time, "sleep"):
            bad = self.login("nope", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn("Set-Cookie", bad.headers)
        ok = self.login(headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(ok.status_code, 303)
        cookie = ok.headers["Set-Cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(flag, cookie)
        self.assertEqual(self.client.get("/players", base_url=PUBLIC).status_code, 200)

    def test_post_without_login_is_rejected(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        r = self.client.post("/day/2026-01-01/save", data={}, base_url=PUBLIC)
        self.assertEqual(r.status_code, 401)

    def test_cross_origin_post_is_rejected_even_when_logged_in(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        self.login()
        r = self.client.post("/logout", base_url=PUBLIC, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/players", base_url=PUBLIC).status_code, 200)

    def test_logout_clears_session(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        self.login()
        self.client.post("/logout", base_url=PUBLIC, headers={"Origin": PUBLIC})
        self.assertEqual(self.client.get("/players", base_url=PUBLIC).status_code, 303)

    def test_brute_force_lockout_per_ip(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        with patch.object(admin_ui.time, "sleep"):
            for _ in range(admin_ui.LOGIN_MAX_FAILS):
                self.assertEqual(self.login("wrong").status_code, 401)
            locked = self.login()  # даже верный пароль не принимается
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Retry-After", locked.headers)
        # другой IP не заблокирован
        other = self.login(environ_overrides={"REMOTE_ADDR": "9.9.9.9"})
        self.assertEqual(other.status_code, 303)

    def test_next_cannot_redirect_off_site(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        for evil in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
            r = self.client.post("/login", base_url=PUBLIC, data={"password": "s3cret-pass", "next": evil})
            self.assertEqual(r.headers["Location"], "/", evil)

    def test_changing_password_invalidates_old_sessions(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        self.login()
        os.environ["ADMIN_PASSWORD"] = "another-pass"
        self.assertEqual(self.client.get("/players", base_url=PUBLIC).status_code, 303)

    def test_expired_and_forged_cookies_rejected(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        expired = admin_ui._sign(int(time.time()) - 5)
        self.assertFalse(admin_ui._cookie_valid(expired))
        self.assertFalse(admin_ui._cookie_valid(f"{int(time.time()) + 999}.deadbeef"))
        self.assertFalse(admin_ui._cookie_valid("garbage"))
        self.assertFalse(admin_ui._cookie_valid(None))

    def test_login_page_escapes_next(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        r = self.client.get('/login?next=/x"><script>alert(1)</script>', base_url=PUBLIC)
        self.assertNotIn(b"<script>alert(1)</script>", r.data)

    def test_security_headers(self):
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
        r = self.client.get("/login", base_url=PUBLIC)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
