"""HEAD on /poster answers like GET, without a body.

Clients probe a poster URL with HEAD before using it (AIOStreams' poster
redirect API, for one) and treat anything but 2xx as "no poster", falling back
to the original art. A GET-only route answers those probes 405.
"""

import unittest

from main import app


class PosterHeadTests(unittest.TestCase):
    def test_poster_route_accepts_head(self):
        methods = set()
        for route in app.routes:
            if getattr(route, "path", None) == "/poster":
                methods |= set(getattr(route, "methods", ()) or ())
        self.assertIn("GET", methods)
        self.assertIn("HEAD", methods)
