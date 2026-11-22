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
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY", "ADMIN_SESSION_DAYS", "ADMIN_SUPER_PASSWORD"):
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


class MegaAdminTests(unittest.TestCase):
    """Две роли на одном экране входа: обычный пароль и пароль мега-админа (по умолчанию admin1).
    «Чаты» — меню, страницы, ленты и отправка — только у мега-админа."""

    def setUp(self):
        admin_ui._fails.clear()
        admin_ui._secret_cache = b"\x02" * 32
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY", "ADMIN_SESSION_DAYS", "ADMIN_SUPER_PASSWORD"):
            os.environ.pop(key, None)
        os.environ["ADMIN_PASSWORD"] = "s3cret-pass"
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

    def login(self, password, client=None):
        return (client or self.client).post("/login", data={"password": password}, base_url=PUBLIC)

    def get(self, path, client=None):
        return (client or self.client).get(path, base_url=PUBLIC)

    def nav(self):
        return self.get("/players").get_data(as_text=True)

    # --- обычный админ: всё, кроме «Чатов» ---------------------------------
    def test_regular_admin_has_no_chats_tab_and_pages_are_404(self):
        r = self.login("s3cret-pass")
        self.assertEqual(r.status_code, 303)
        cookies = r.headers.get_all("Set-Cookie")
        self.assertTrue(any(c.startswith("admin_auth=") and "Max-Age" in c for c in cookies))
        self.assertFalse(any(c.startswith("admin_super=") and "Max-Age=0" not in c and "Expires=Thu, 01 Jan 1970" not in c
                             for c in cookies))
        page = self.nav()
        self.assertIn(">Игроки</a>", page)
        self.assertNotIn(">Чаты</a>", page)
        for path in ("/chats", "/chats/123", "/chats/123/feed", "/chats/version"):
            self.assertEqual(self.get(path).status_code, 404, path)
        for path in ("/chats/123/send", "/chats/sync", "/chats/123/sync"):
            self.assertEqual(self.client.post(path, data={"text": "hi"}, base_url=PUBLIC).status_code, 404, path)

    # --- мега-админ ---------------------------------------------------------
    def test_default_super_password_is_admin1_and_opens_chats(self):
        r = self.login("admin1")
        self.assertEqual(r.status_code, 303)
        names = {c.split("=", 1)[0] for c in r.headers.get_all("Set-Cookie")}
        self.assertEqual(names, {"admin_auth", "admin_super"})
        self.assertIn(">Чаты</a>", self.nav())
        self.assertEqual(self.get("/chats").status_code, 200)
        self.assertEqual(self.get("/players").status_code, 200)

    def test_super_password_can_be_set_in_env(self):
        os.environ["ADMIN_SUPER_PASSWORD"] = "boss-pass"
        with patch.object(admin_ui.time, "sleep"):
            self.assertEqual(self.login("admin1").status_code, 401)   # старый пароль больше не подходит
        self.assertEqual(self.login("boss-pass").status_code, 303)
        self.assertIn(">Чаты</a>", self.nav())

    def test_empty_super_password_disables_the_role(self):
        os.environ["ADMIN_SUPER_PASSWORD"] = ""
        with patch.object(admin_ui.time, "sleep"):
            self.assertEqual(self.login("admin1").status_code, 401)
            self.assertEqual(self.login("").status_code, 401)
        self.login("s3cret-pass")
        self.assertNotIn(">Чаты</a>", self.nav())
        self.assertEqual(self.get("/chats").status_code, 404)

    def test_logging_in_with_regular_password_drops_super_rights(self):
        self.login("admin1")
        self.assertEqual(self.get("/chats").status_code, 200)
        self.login("s3cret-pass")
        self.assertEqual(self.get("/chats").status_code, 404)
        self.assertNotIn(">Чаты</a>", self.nav())

    def test_changing_super_password_drops_only_super_rights(self):
        self.login("admin1")
        os.environ["ADMIN_SUPER_PASSWORD"] = "new-boss"
        self.assertEqual(self.get("/chats").status_code, 404)
        self.assertEqual(self.get("/players").status_code, 200)     # обычный вход жив

    def test_logout_clears_both_cookies(self):
        self.login("admin1")
        self.client.post("/logout", base_url=PUBLIC, headers={"Origin": PUBLIC})
        self.assertEqual(self.get("/chats").status_code, 303)       # на экран входа
        self.assertEqual(self.get("/players").status_code, 303)

    def test_regular_cookie_cannot_pass_as_super_cookie(self):
        expires = int(time.time()) + 600
        regular = admin_ui._sign(expires)
        forged = admin_ui.app.test_client(use_cookies=False)
        r = forged.get("/chats", base_url=PUBLIC, headers={"Cookie": f"admin_auth={regular}; admin_super={regular}"})
        self.assertEqual(r.status_code, 404)
        real = admin_ui._sign(expires, admin_ui.SUPER_ROLE)
        r = forged.get("/chats", base_url=PUBLIC, headers={"Cookie": f"admin_auth={regular}; admin_super={real}"})
        self.assertEqual(r.status_code, 200)

    def test_super_cookie_alone_does_not_log_in(self):
        real = admin_ui._sign(int(time.time()) + 600, admin_ui.SUPER_ROLE)
        alone = admin_ui.app.test_client(use_cookies=False)
        r = alone.get("/chats", base_url=PUBLIC, headers={"Cookie": f"admin_super={real}"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["Location"])

    def test_super_password_attempts_count_toward_lockout(self):
        os.environ["ADMIN_SUPER_PASSWORD"] = "boss-pass"
        with patch.object(admin_ui.time, "sleep"):
            for _ in range(admin_ui.LOGIN_MAX_FAILS):
                self.assertEqual(self.login("admin2").status_code, 401)
            self.assertEqual(self.login("boss-pass").status_code, 429)

    # --- без ADMIN_PASSWORD (localhost / SSH-туннель) ------------------------
    def test_local_mode_without_password_is_full_access(self):
        os.environ.pop("ADMIN_PASSWORD")
        local = "http://localhost:8787"
        page = self.client.get("/players", base_url=local).get_data(as_text=True)
        self.assertIn(">Чаты</a>", page)
        self.assertEqual(self.client.get("/chats", base_url=local).status_code, 200)
        self.assertEqual(self.client.get("/chats", base_url=PUBLIC).status_code, 403)


if __name__ == "__main__":
    unittest.main()
