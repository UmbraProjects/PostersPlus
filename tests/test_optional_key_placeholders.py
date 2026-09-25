"""An unsubstituted key placeholder is "no key", not a key.

The configurator copies "{tmdb_key?}" / "{mdblist_key?}" for clients that
implement the optional form, so a user with no key in AIOMetadata still gets a
poster. A build that leaves the placeholder verbatim, or a resolver with no key
placeholders at all, must fall through to the server's key rather than sending
the literal to TMDB / MDBList.
"""

import unittest
from unittest import mock

import main


class KeyPlaceholderTests(unittest.TestCase):
    def test_literal_tmdb_key_falls_back_to_the_server_key(self):
        with mock.patch.object(main._cfg, "SERVER_TMDB_KEY", "server-tmdb"):
            for literal in ("{tmdb_key}", "{tmdb_key?}", "", " "):
                with self.subTest(literal=literal):
                    self.assertEqual(main._resolve_tmdb_key(literal), "server-tmdb")
            self.assertEqual(main._resolve_tmdb_key("user-tmdb"), "user-tmdb")

    def test_literal_tmdb_key_on_a_keyless_instance_is_none(self):
        with mock.patch.object(main._cfg, "SERVER_TMDB_KEY", ""):
            self.assertIsNone(main._resolve_tmdb_key("{tmdb_key?}"))

    def test_literal_mdblist_key_falls_back_to_the_server_key(self):
        with mock.patch.object(main._cfg, "SERVER_MDBLIST_KEYS", ["server-mdb"]):
            for literal in ("{mdblist_key}", "{mdblist_key?}", ""):
                with self.subTest(literal=literal):
                    self.assertEqual(main._resolve_mdblist_key(literal), "server-mdb")
            self.assertEqual(main._resolve_mdblist_key("user-mdb"), "user-mdb")

    def test_literal_mdblist_key_on_a_keyless_instance_is_none(self):
        with mock.patch.object(main._cfg, "SERVER_MDBLIST_KEYS", []):
            self.assertIsNone(main._resolve_mdblist_key("{mdblist_key?}"))


if __name__ == "__main__":
    unittest.main()
