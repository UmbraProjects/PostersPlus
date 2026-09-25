"""The configurator's header Admin link: off unless the operator turns on
SHOW_ADMIN_LINK, and never shown while the dashboard itself is disabled."""
from pathlib import Path
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import admin
import config as _cfg
import main


class AdminLinkCapsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def _caps(self, show: bool, key: str) -> dict:
        with mock.patch.object(_cfg, "SHOW_ADMIN_LINK", show), \
             mock.patch.object(_cfg, "ACCESS_KEY", None), \
             mock.patch.object(admin, "ADMIN_KEY", key):
            return self.client.get("/server-caps").json()

    def test_off_by_default(self):
        self.assertIs(_cfg.SHOW_ADMIN_LINK, False)
        self.assertFalse(self._caps(False, "a-long-admin-key")["admin_link"])

    def test_on_when_the_operator_asks_and_the_dashboard_is_enabled(self):
        self.assertTrue(self._caps(True, "a-long-admin-key")["admin_link"])

    def test_hidden_while_the_dashboard_is_disabled(self):
        self.assertFalse(self._caps(True, "")["admin_link"])


class AdminLinkConfiguratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_link_starts_hidden_and_follows_server_caps(self):
        self.assertRegex(self.html, r'<a class="header-admin" id="header-admin" href="/admin"[^>]* hidden>')
        self.assertIn(".header-admin[hidden] { display: none; }", self.html)
        self.assertIn("document.getElementById('header-admin').hidden = !serverCaps.admin_link;", self.html)


if __name__ == "__main__":
    unittest.main()
