"""Every operator setting is declared once in config.py; these tests keep the
three documents that describe them from drifting away from that registry.

  CONFIGURATION.md  generated reference block (tools/settings_docs.py --write)
  .env.example   every everyday (non-advanced) setting has a KEY= line
  ADVANCED.md    every advanced setting has a `### KEY` section

and no module reads an operator setting from os.environ behind the
registry's back, which would make the admin dashboard show a field that
does nothing.
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import config  # noqa: F401 — populates the registry
import settings as _settings

ROOT = Path(__file__).resolve().parents[1]

# Read from the environment on purpose, outside the registry.
ENV_ONLY = {
    "ADMIN_KEY",       # protects the dashboard, so cannot be managed by it
    "SETTINGS_PATH",   # where the dashboard's file lives
    "TMDB_LANGUAGE",   # legacy alias for DEFAULT_LOGO_LANGUAGE
    "WORKERS",         # declared in the registry too; entrypoint.sh reads it
}
# Not runtime settings at all.
BUILD_ONLY = {"BAKE_PPOCR_MODEL"}


class ConfigurationReferenceTests(unittest.TestCase):
    def test_configuration_reference_matches_the_registry(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "settings_docs.py"), "--check"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class EnvExampleTests(unittest.TestCase):
    def test_every_everyday_setting_has_a_line(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        present = set(re.findall(r"^([A-Z0-9_]+)=", text, re.M))
        missing = sorted(
            k for k, s in _settings.REGISTRY.items() if not s.advanced and k not in present
        )
        self.assertEqual(missing, [], f".env.example is missing everyday settings: {missing}")

    def test_no_unknown_keys(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        present = set(re.findall(r"^([A-Z0-9_]+)=", text, re.M))
        unknown = sorted(present - set(_settings.REGISTRY) - ENV_ONLY - BUILD_ONLY)
        self.assertEqual(unknown, [], f".env.example lists keys nothing reads: {unknown}")


class AdvancedDocTests(unittest.TestCase):
    def test_every_advanced_setting_has_a_section(self):
        text = (ROOT / "ADVANCED.md").read_text(encoding="utf-8")
        headed = set(re.findall(r"^### `([A-Z0-9_]+)`", text, re.M))
        missing = sorted(k for k, s in _settings.REGISTRY.items() if s.advanced and k not in headed)
        self.assertEqual(missing, [], f"ADVANCED.md has no section for: {missing}")

    def test_no_section_for_a_setting_that_does_not_exist(self):
        text = (ROOT / "ADVANCED.md").read_text(encoding="utf-8")
        headed = set(re.findall(r"^### `([A-Z0-9_]+)`", text, re.M))
        unknown = sorted(headed - set(_settings.REGISTRY) - ENV_ONLY - BUILD_ONLY)
        self.assertEqual(unknown, [], f"ADVANCED.md documents keys nothing reads: {unknown}")


class NoBypassTests(unittest.TestCase):
    def test_no_module_reads_an_operator_setting_directly(self):
        # The sync scripts are standalone tools with their own PLEX_* /
        # JELLYFIN_* / POSTERSPLUS_* variables, not part of the service.
        skip = {"config.py", "settings.py", "plex_sync.py", "jellyfin_sync.py"}
        offenders = []
        for path in ROOT.glob("*.py"):
            if path.name in skip:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                m = re.search(r'os\.environ(?:\.get\(|\[)\s*"([A-Z0-9_]+)"', line)
                if m and m.group(1) not in ENV_ONLY:
                    offenders.append(f"{path.name}:{lineno} {m.group(1)}")
        self.assertEqual(offenders, [], f"read os.environ instead of the registry: {offenders}")


if __name__ == "__main__":
    unittest.main()
