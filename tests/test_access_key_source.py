"""The configurator's access key comes from its own URL, never from storage.

When the server has an ACCESS_KEY the page can't load without the right one in
its URL, so a remembered copy is never needed.  It was kept in localStorage,
outlived the server dropping or changing its key, and went into every copied
URL.  /server-caps says whether a key is required, so one left in an old
bookmark is dropped too.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import main

HTML = (Path(__file__).resolve().parent.parent / "configurator.html").read_text(encoding="utf-8")


class ServerCapsReportsAccessKey(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_open_instance_says_no_key_is_required(self):
        with mock.patch.object(main._cfg, "ACCESS_KEY", None):
            caps = self.client.get("/server-caps").json()
        self.assertIs(caps["access_key_required"], False)

    def test_keyed_instance_says_a_key_is_required(self):
        with mock.patch.object(main._cfg, "ACCESS_KEY", "sekrit"):
            caps = self.client.get("/server-caps?access_key=sekrit").json()
        self.assertIs(caps["access_key_required"], True)


class ConfiguratorReadsTheUrlOnly(unittest.TestCase):
    def test_access_key_is_never_read_from_or_written_to_storage(self):
        self.assertIsNone(re.search(r"localStorage\.(getItem|setItem)\('postersplus_access_key'", HTML))
        self.assertIn("localStorage.removeItem('postersplus_access_key')", HTML)

    def test_key_is_cleared_when_the_server_requires_none(self):
        self.assertIn("serverCaps.access_key_required === false", HTML)


if __name__ == "__main__":
    unittest.main()
