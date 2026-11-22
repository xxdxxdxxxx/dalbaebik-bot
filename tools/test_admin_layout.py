"""Вёрстка админ-панели: шапки таблиц в прокручиваемых обёртках и страница «Игроки» без подписи периода."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import player_store
from tools import admin_ui


class StickyHeaderCssTests(unittest.TestCase):
    """
    Шапка таблицы липнет к верху страницы со сдвигом на высоту меню (top: var(--head)). Внутри обёртки с
    overflow (.scroll, .panel.xscroll, а на узких экранах и .panel) «верх» — это верх обёртки, и тот же сдвиг
    уводил шапку на 52px вниз: она закрывала первые строки («Заход в войс», «Исходные табы»).
    """
    css = admin_ui.BASE_CSS

    def test_plain_tables_keep_sticking_under_the_menu(self):
        self.assertIn("top:var(--head)", re.search(r"\nth\{[^}]*\}", self.css).group(0))

    def test_header_inside_scroll_wrappers_sticks_to_the_wrapper_top(self):
        self.assertIn(".scroll th,.panel.xscroll th{top:0}", self.css)

    def test_panel_turns_into_a_scroll_wrapper_on_narrow_screens_and_resets_the_offset_there(self):
        self.assertIn("@media (max-width:900px){.panel{overflow-x:auto}.panel th{top:0}}", self.css)


class PlayersCaptionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("ADMIN_PASSWORD", "ADMIN_TRUST_PROXY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "scan_stats.sqlite3"
        player_store.ensure_schema(self.db)
        self.patch = patch.object(admin_ui, "DB_PATH", self.db)
        self.patch.start()
        self.client = admin_ui.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def test_players_page_has_no_period_caption_under_the_title(self):
        html = self.client.get("/players", base_url="http://localhost:8787").get_data(as_text=True)
        self.assertIn("<h1>Игроки</h1>", html)
        self.assertNotIn("<div class='subtitle'>", html)


if __name__ == "__main__":
    unittest.main()
