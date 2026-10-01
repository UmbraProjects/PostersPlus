"""The composite cache's assets signature follows what the language files and
genre backgrounds contain, not when they were written.  Every image build
checks the repo out afresh and restamps every file, so an mtime signature
re-rendered every cached poster on each update."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import main


class RenderAssetsSignatureTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.base = self._dir.name
        self.write("languages/en.json", b'{"a": 1}')
        self.write("static/genre_bg/minimal/Action.png", b"png-bytes")
        self.write("static/genre_bg/classic/Drama.png", b"other-bytes")
        patches = (
            mock.patch.object(main, "BASE_DIR", self.base),
            mock.patch.object(main, "_asset_digests", {}),
            mock.patch.object(main.discovery, "override_path",
                              return_value=os.path.join(self.base, "missing.json")),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._dir.cleanup)

    def write(self, rel, data, base=None):
        path = os.path.join(base or self.base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def sig(self):
        return main._compute_render_assets_signature()

    def test_restamped_files_keep_the_signature(self):
        before = self.sig()
        for dirpath, _, files in os.walk(self.base):
            for f in files:
                os.utime(os.path.join(dirpath, f), ns=(1, 1))
        self.assertEqual(self.sig(), before)

    def test_a_fresh_copy_of_the_same_tree_matches(self):
        # What a new checkout in a new build is: same bytes, new paths' stamps.
        before = self.sig()
        copy = os.path.join(self.base, "..", os.path.basename(self.base) + "-copy")
        shutil.copytree(self.base, copy)
        self.addCleanup(shutil.rmtree, copy)
        # copytree keeps the originals' mtimes; a checkout doesn't.
        for dirpath, _, files in os.walk(copy):
            for f in files:
                os.utime(os.path.join(dirpath, f), ns=(2, 2))
        with mock.patch.object(main, "BASE_DIR", copy), \
             mock.patch.object(main, "_asset_digests", {}):
            self.assertEqual(self.sig(), before)

    def test_changed_added_or_renamed_assets_change_it(self):
        before = self.sig()
        path = self.write("languages/en.json", b'{"a": 2}')
        changed = self.sig()
        self.assertNotEqual(changed, before)
        self.write("languages/fr.json", b"{}")
        added = self.sig()
        self.assertNotEqual(added, changed)
        os.rename(path, os.path.join(self.base, "languages", "en-GB.json"))
        self.assertNotEqual(self.sig(), added)

    def test_same_size_same_mtime_edit_is_still_seen_after_a_restart(self):
        # The per-file cache trusts (size, mtime) only within one process.
        before = self.sig()
        path = os.path.join(self.base, "static/genre_bg/minimal/Action.png")
        st = os.stat(path)
        self.write("static/genre_bg/minimal/Action.png", b"PNG-BYTES")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        with mock.patch.object(main, "_asset_digests", {}):
            self.assertNotEqual(self.sig(), before)

    def test_the_sash_lists_file_still_counts(self):
        before = self.sig()
        lists = self.write("lists.json", b'{"cast": {}}')
        with mock.patch.object(main.discovery, "override_path", return_value=lists):
            self.assertNotEqual(self.sig(), before)


if __name__ == "__main__":
    unittest.main()
