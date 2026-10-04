"""Operator presets: what is kept from a pasted URL, the list's limits, the
preview images, the endpoints' gates, and the configurator's three tabs."""
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

import admin
import config as _cfg
import main
import presets

HTML = (pathlib.Path(__file__).resolve().parents[1] / "configurator.html").read_text()
ADMIN_HTML = (pathlib.Path(__file__).resolve().parents[1] / "admin.html").read_text()


def _png(w=300, h=450) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = {}
        for p in (
            mock.patch.object(presets, "get_app_state", self.state.get),
            mock.patch.object(presets, "set_app_state", self.state.__setitem__),
            mock.patch.object(_cfg, "PRESET_ART_DIR", self.dir.name),
        ):
            p.start()
            self.addCleanup(p.stop)


class CleanParamsTests(unittest.TestCase):
    def test_keys_and_title_are_dropped(self):
        url = ("https://posters.example.com/poster?tmdb_id=603&type=movie&imdb_id=tt0133093"
               "&access_key=SECRET&tmdb_key=T&mdblist_key=M&fanart_key=F&my_api_token=x"
               "&rating_display_mode=3&sash_mode=notch")
        params, shape = presets.clean_params(url)
        self.assertEqual(params, "rating_display_mode=3&sash_mode=notch")
        self.assertEqual(shape, "portrait")
        for secret in ("SECRET", "tmdb_key", "603", "tt0133093", "token"):
            self.assertNotIn(secret, params)

    def test_case_and_duplicates_cannot_sneak_a_key_through(self):
        params, _ = presets.clean_params("ACCESS_KEY=x&Access_Key=y&sash_mode=notch&sash_mode=sash")
        self.assertEqual(params, "sash_mode=notch")

    def test_odd_names_and_control_characters(self):
        params, _ = presets.clean_params("a%3Cb=1&x-y=2&sash_mode=no%E2%80%AEtch%00")
        self.assertEqual(params, "sash_mode=notch")

    def test_shape(self):
        self.assertEqual(presets.clean_params("shape=landscape&badge_pos=top_left")[1], "landscape")
        self.assertEqual(presets.clean_params("shape=%7Bshape%7D&badge_pos=top_left"),
                         ("shape=%7Bshape%7D&badge_pos=top_left", "portrait"))
        self.assertEqual(presets.clean_params("shape=square&badge_pos=top_left"),
                         ("badge_pos=top_left", "portrait"))

    def test_nothing_left(self):
        for raw in ("", "https://x/poster?tmdb_id=1&access_key=k", "shape=landscape", "shape=%7Bshape%7D"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                presets.clean_params(raw)


class StoreTests(_Case):
    def test_save_edit_delete(self):
        items = presets.save({"name": "Mine", "description": "d", "params": "sash_mode=notch&access_key=k"})
        self.assertEqual(len(items), 1)
        pid = items[0]["id"]
        self.assertRegex(pid, r"^[0-9a-f]{12}$")
        self.assertNotIn("access_key", self.state[presets.STATE_KEY])
        presets.save({"id": pid, "name": "Renamed", "params": "sash_mode=sash"})
        self.assertEqual([p["name"] for p in presets.list_presets()], ["Renamed"])
        with self.assertRaises(ValueError):
            presets.save({"id": "000000000000", "name": "x", "params": "sash_mode=sash"})
        presets.delete(pid)
        self.assertEqual(presets.list_presets(), [])

    def test_text_is_cleaned_and_capped(self):
        items = presets.save({"name": "  A‮B\n" + "x" * 100, "description": "y" * 999,
                              "params": "sash_mode=notch"})
        self.assertEqual(items[0]["name"], ("AB " + "x" * 100)[:presets.MAX_NAME])
        self.assertEqual(len(items[0]["description"]), presets.MAX_DESCRIPTION)
        with self.assertRaises(ValueError):
            presets.save({"name": " ​ ", "params": "sash_mode=notch"})

    def test_limit(self):
        with mock.patch.object(presets, "MAX_PRESETS", 2):
            presets.save({"name": "a", "params": "sash_mode=notch"})
            presets.save({"name": "b", "params": "sash_mode=notch"})
            with self.assertRaises(ValueError):
                presets.save({"name": "c", "params": "sash_mode=notch"})

    def test_reorder(self):
        ids = [presets.save({"name": n, "params": "sash_mode=notch"})[-1]["id"] for n in "abc"]
        presets.reorder([ids[2], ids[0]])
        self.assertEqual([p["name"] for p in presets.list_presets()], ["c", "a", "b"])

    def test_image_must_be_one_stored_here(self):
        for image in ("../../etc/passwd", "0123456789abcdef.jpg", "/preset-art/x.jpg"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                presets.save({"name": "a", "params": "sash_mode=notch", "image": image})
        name = presets.store_image(_png())
        items = presets.save({"name": "a", "params": "sash_mode=notch", "image": name})
        self.assertEqual(presets.public_list()[0]["screenshot"], f"/preset-art/{name}")
        self.assertNotIn("created", presets.public_list()[0])
        # Dropped with the preset that used it.
        presets.delete(items[0]["id"])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, name)))

    def test_images_are_reencoded_and_bounded(self):
        name = presets.store_image(_png(3000, 4500))
        with Image.open(os.path.join(self.dir.name, name)) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertLessEqual(max(im.size), 720)
        for data in (b"", b"<svg onload=alert(1)>", _png(20, 20)):
            with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                presets.store_image(data)
        self.assertIsNone(presets.image_bytes("../cache.db"))

    def test_fresh_uploads_survive_a_sweep(self):
        name = presets.store_image(_png())
        presets.save({"name": "a", "params": "sash_mode=notch"})
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, name)))


class EndpointTests(_Case):
    KEY = "a-long-admin-key"

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(admin, "ADMIN_KEY", self.KEY),
                  mock.patch.object(_cfg, "ACCESS_KEY", "instance-key")):
            p.start()
            self.addCleanup(p.stop)

    def h(self):
        return {"X-Admin-Key": self.KEY}

    def test_writes_need_the_admin_key(self):
        for method, path in (("put", "/admin/api/presets"), ("put", "/admin/api/presets/order"),
                             ("delete", "/admin/api/presets?id=x"), ("post", "/admin/api/presets/image"),
                             ("get", "/admin/api/presets")):
            with self.subTest(path=path):
                r = getattr(self.client, method)(path, headers={"X-Admin-Key": "wrong"})
                self.assertIn(r.status_code, (401, 403))

    def test_round_trip_to_server_caps(self):
        r = self.client.put("/admin/api/presets", headers=self.h(), json={
            "name": "Ours", "description": "d",
            "params": "https://p.example/poster?tmdb_id=1&type=movie&access_key=instance-key&sash_mode=notch"})
        self.assertEqual(r.status_code, 200, r.text)
        caps = self.client.get("/server-caps?access_key=instance-key").json()
        self.assertEqual([p["name"] for p in caps["operator_presets"]], ["Ours"])
        self.assertEqual(caps["operator_presets"][0]["params"], "sash_mode=notch")
        self.assertEqual(self.client.get("/server-caps").status_code, 403)

    def test_a_preset_without_a_description_is_listed(self):
        for body in ({"name": "Bare", "params": "sash_mode=notch"},
                     {"name": "Blank", "description": "   ", "params": "sash_mode=notch"}):
            r = self.client.put("/admin/api/presets", headers=self.h(), json=body)
            self.assertEqual(r.status_code, 200, r.text)
        caps = self.client.get("/server-caps?access_key=instance-key").json()
        listed = {p["name"]: p["description"] for p in caps["operator_presets"]}
        self.assertEqual(listed, {"Bare": None, "Blank": None})

    def test_bad_bodies(self):
        self.assertEqual(self.client.put("/admin/api/presets", headers=self.h(), content=b"[1]").status_code, 400)
        self.assertEqual(self.client.put("/admin/api/presets", headers=self.h(),
                                         content=b"{" + b" " * 40000 + b"}").status_code, 413)
        self.assertEqual(self.client.put("/admin/api/presets", headers=self.h(),
                                         json={"name": "x", "params": "tmdb_id=1"}).status_code, 400)

    def test_image_upload_and_serving(self):
        r = self.client.post("/admin/api/presets/image", headers=self.h(), content=_png())
        self.assertEqual(r.status_code, 200, r.text)
        served = self.client.get(r.json()["url"])
        self.assertEqual(served.headers["content-type"], "image/jpeg")
        self.assertEqual(served.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/preset-art/..%2Fcache.db").status_code, 404)
        bad = self.client.post("/admin/api/presets/image", headers=self.h(), content=b"<html>")
        self.assertEqual(bad.status_code, 400)


class PageTests(unittest.TestCase):
    def test_cards_never_use_innerhtml_for_names(self):
        card = HTML.split("function _buildPresetCard(", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("innerHTML", card)
        self.assertIn("name.textContent = preset.name", card)

    def test_three_tabs_and_param_scrub(self):
        self.assertIn("['core', 'Core']", HTML)
        self.assertIn("'operator', 'Operator'", HTML)
        self.assertIn("['user', 'Mine']", HTML)
        # Every preset's params go through the scrub before they're imported.
        load = HTML.split("function loadPreset(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("_presetParams(preset.params)", load)
        self.assertIn("/key|token|secret|password|auth/", HTML)

    def test_only_safe_pictures(self):
        self.assertIn(r"/^\/preset-art\/[0-9a-f]{16}\.jpg$/", HTML)
        self.assertIn(r"/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/", HTML)

    def test_imports_are_kept_as_a_preset(self):
        apply = HTML.split("function applyImport(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("_rememberImport(raw)", apply)
        remember = HTML.split("function _rememberImport(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("_presetParams(", remember)
        # The reset menu offers the import beside the defaults.
        self.assertIn('id="reset-menu-import"', HTML)
        self.assertIn("resetScopeToImport(target.roots, target.name)", HTML)

    def test_dashboard_escapes_preset_text(self):
        view = ADMIN_HTML.split("function renderPresets()", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("esc(p.name)", view)
        self.assertIn("esc(p.description)", view)
        self.assertIn("jsArg(p.id)", view)


if __name__ == "__main__":
    unittest.main()
