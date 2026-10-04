"""One instance following another's artwork overrides (ART_OVERRIDES_SHARE /
ART_OVERRIDES_REMOTE_URL)."""

import asyncio
import hashlib
import io
import sqlite3
import tempfile
import unittest
from unittest import mock

import httpx
from PIL import Image

import art_overrides as ao
import main


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 90), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class RemoteBaseTests(unittest.TestCase):
    def test_site_or_any_page_on_it(self):
        for typed, want in (
            ("https://postersplus.example", "https://postersplus.example"),
            ("https://postersplus.example/", "https://postersplus.example"),
            ("https://postersplus.example/admin#artwork", "https://postersplus.example"),
            ("https://host.example/pp/admin#artwork/movie/1", "https://host.example/pp"),
            ("http://lan:8000/configure?x=1", "http://lan:8000"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(ao.remote_base(typed), want)

    def test_not_a_site(self):
        for bad in ("", "postersplus.example", "ftp://x.example", "https://u:p@x.example"):
            with self.subTest(bad=bad):
                self.assertIsNone(ao.remote_base(bad))


class RemoteRowTests(unittest.TestCase):
    def test_rows_are_validated_like_a_dashboard_write(self):
        good = {"media_type": "series", "tmdb_id": "1399", "slot": "textless", "language": "",
                "path": "/a.jpg", "sources": ["tmdb"], "crop": "0.2,0.5,1.25", "updated_at": 5}
        row = ao._remote_row(good)
        self.assertEqual(row[:7], ("tv", "1399", "textless", "", "/a.jpg", "tmdb", "tmdb"))
        self.assertEqual(row[9], ao.Crop(0.2, 0.5, 1.25).token())
        for bad in (
            {**good, "path": "https://evil.example/x.jpg"},
            {**good, "slot": "nope"},
            {**good, "tmdb_id": "../1"},
            {**good, "sources": []},
            {**good, "slot": "logo", "language": "en", "crop": "0.5,0.5,1"},
            {**good, "slot": "original", "language": "not a language"},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(ao._remote_row(bad))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("""
            CREATE TABLE art_overrides (
                media_type TEXT NOT NULL, tmdb_id TEXT NOT NULL, slot TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
                provider TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL, crop TEXT,
                PRIMARY KEY (media_type, tmdb_id, slot, language))
        """)
        self.db.execute("CREATE TABLE art_overrides_remote AS SELECT * FROM art_overrides WHERE 0")
        self.state = {}
        self.invalidated = []
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patches = [
            mock.patch.object(ao, "get_db", lambda: self.db),
            mock.patch.object(ao, "get_app_state", self.state.get),
            mock.patch.object(ao, "set_app_state", self.state.__setitem__),
            mock.patch.object(
                ao, "invalidate_final_posters",
                lambda tid, mt=None, l1_only=False: self.invalidated.append((tid, mt))),
            mock.patch.object(ao, "_snapshot", {}),
            mock.patch.object(ao, "_rev", None),
            mock.patch.object(ao, "_checked_at", float("-inf")),
            mock.patch.object(ao._cfg, "CUSTOM_ART_DIR", self.dir.name),
            mock.patch.object(ao._cfg, "ART_OVERRIDES_REMOTE_URL", "https://share.example/admin#artwork"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.image = _jpeg()
        self.custom = f"custom:{hashlib.sha256(self.image).hexdigest()[:16]}.jpg"

    def _export(self, overrides, rev="r1"):
        return {"version": ao.EXPORT_VERSION, "rev": rev, "overrides": overrides}

    def _client(self, export, image=None, seen=None):
        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append(request)
            if request.url.path == "/art-overrides/export.json":
                if request.headers.get("if-none-match") == f'"{export["rev"]}"':
                    return httpx.Response(304)
                return httpx.Response(200, json=export)
            if request.url.path.startswith("/custom-art/"):
                return httpx.Response(200, content=image if image is not None else self.image)
            return httpx.Response(404)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def _sync(self, client):
        async def go():
            async with client:
                await ao.sync_remote(client)
        asyncio.run(go())

    def test_followed_rows_apply_and_local_ones_win(self):
        export = self._export([
            {"media_type": "movie", "tmdb_id": "550", "slot": "textless", "language": "",
             "path": "/remote.jpg", "sources": ["tmdb"], "updated_at": 1},
            {"media_type": "movie", "tmdb_id": "550", "slot": "logo", "language": "en",
             "path": "/rlogo.png", "sources": [], "updated_at": 1},
        ])
        self._sync(self._client(export))
        entry = ao.for_title("movie", "550")
        self.assertEqual(entry["textless"][""].path, "/remote.jpg")
        self.assertTrue(entry["textless"][""].remote)
        self.assertIn(("550", "movie"), self.invalidated)
        # The dashboard lists only this instance's own.
        self.assertEqual(ao.list_overrides(), [])
        ao.set_override("movie", "550", "textless", "", "/local.jpg", ["tmdb"])
        entry = ao.for_title("movie", "550")
        self.assertEqual(entry["textless"][""].path, "/local.jpg")
        self.assertEqual(entry["logo"]["en"].path, "/rlogo.png")   # the slot it didn't replace
        remote = {o["slot"]: o["replaced"] for o in ao.remote_title_overrides("movie", "550")}
        self.assertEqual(remote, {"textless": True, "logo": False})

    def test_unchanged_export_is_not_refetched(self):
        export = self._export([])
        self._sync(self._client(export))
        seen = []
        self._sync(self._client(export, seen=seen))
        self.assertEqual(seen[0].headers.get("if-none-match"), '"r1"')
        self.assertIsNone(ao.remote_state()["error"])

    def test_custom_images_are_copied_only_when_they_hash_to_their_name(self):
        row = {"media_type": "movie", "tmdb_id": "550", "slot": "textless", "language": "",
               "path": self.custom, "sources": ["tmdb"], "updated_at": 1}
        self._sync(self._client(self._export([row]), image=b"not what the name says"))
        self.assertIsNone(ao.for_title("movie", "550"))
        self.assertEqual(ao.remote_state()["skipped"], 1)
        self._sync(self._client(self._export([row], rev="r2")))
        self.assertEqual(ao.for_title("movie", "550")["textless"][""].path, self.custom)
        self.assertEqual(ao.custom_art_bytes(self.custom), self.image)
        # Dropped from the export: dropped here, and the copy with it.
        self._sync(self._client(self._export([], rev="r3")))
        self.assertIsNone(ao.for_title("movie", "550"))
        self.assertIsNone(ao.custom_art_bytes(self.custom))

    def test_unfollowing_clears_the_rows(self):
        export = self._export([{"media_type": "movie", "tmdb_id": "550", "slot": "textless",
                                "language": "", "path": "/r.jpg", "sources": ["tmdb"]}])
        self._sync(self._client(export))
        with mock.patch.object(ao._cfg, "ART_OVERRIDES_REMOTE_URL", ""):
            self._sync(self._client(export))
        self.assertIsNone(ao.for_title("movie", "550"))

    def test_a_sharer_that_does_not_share(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        self._sync(client)
        self.assertIn("doesn't share", ao.remote_state()["error"])

    def test_export_lists_only_local_rows(self):
        ao.set_override("movie", "550", "textless", "", "/a.jpg", ["tmdb"], "Fight Club")
        self.db.execute("INSERT INTO art_overrides_remote VALUES "
                        "('tv', '1', 'textless', '', '/r.jpg', 'tmdb', 'tmdb', '', 0, NULL)")
        out = ao.export()
        self.assertEqual([(o["tmdb_id"], o["path"]) for o in out["overrides"]], [("550", "/a.jpg")])
        self.assertEqual(out["overrides"][0]["sources"], ["tmdb"])


class ExportEndpointTests(unittest.TestCase):
    def test_off_unless_shared(self):
        from fastapi.testclient import TestClient
        with TestClient(main.app) as client:
            with mock.patch.object(main._cfg, "ART_OVERRIDES_SHARE", False):
                self.assertEqual(client.get("/art-overrides/export.json").status_code, 404)
            with mock.patch.object(main._cfg, "ART_OVERRIDES_SHARE", True), \
                    mock.patch.object(main.art_overrides, "export",
                                      lambda: {"version": 1, "rev": "abc", "overrides": []}):
                first = client.get("/art-overrides/export.json")
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.headers["etag"], '"abc"')
                again = client.get("/art-overrides/export.json", headers={"If-None-Match": '"abc"'})
                self.assertEqual(again.status_code, 304)


if __name__ == "__main__":
    unittest.main()
