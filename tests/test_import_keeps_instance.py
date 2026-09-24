"""Importing a URL from another instance keeps Copy config on this one.

The configurator's domain and access key belong to the instance serving the
page, read from its own URL at startup.  Taking them from an imported URL meant
a URL pasted in from another instance came back out of Copy config still
pointing at that instance (with its key), which quietly undid a switch.
"""

from pathlib import Path
import re
import unittest


class ImportKeepsDomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = Path("configurator.html").read_text(encoding="utf-8")
        start = html.index("function importUrl(")
        cls.import_body = html[start : html.index("\nfunction ", start + 1)]

    def test_import_never_writes_the_domain_or_access_key(self):
        for field in ("cfg-domain", "cfg-access-key"):
            with self.subTest(field=field):
                self.assertIsNone(re.search(rf"{field}['\"]\)\.value\s*=", self.import_body))
                self.assertNotIn(f"_setEl('{field}'", self.import_body)


if __name__ == "__main__":
    unittest.main()
