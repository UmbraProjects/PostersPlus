import itertools
import json
import re
import unittest
from pathlib import Path

from PIL import ImageFont

import fonts
from festivals import FESTIVAL_SASH_LABELS
from i18n import load_languages, translate_sash, upper_label


LANGUAGE_DIR = Path(__file__).resolve().parents[1] / "languages"

RELEASE_STATUS_LABELS = {
    "Physical",
    "Streaming",
    "Cinema",
    "Production",
    "Airing",
    "Renewed",
    "Ended",
    "Cancelled",
    "Returns",
    "seasonWindow",
}

FIXED_SASH_LABELS = RELEASE_STATUS_LABELS | FESTIVAL_SASH_LABELS


def _load_language(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


class FixedSashVocabularyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_canonical_locale_documents_all_fixed_sash_labels(self):
        english = _load_language(LANGUAGE_DIR / "en.json")
        self.assertTrue(FIXED_SASH_LABELS <= english["sashLabels"].keys())

    def test_every_shipped_translation_includes_all_fixed_sash_labels(self):
        for path in LANGUAGE_DIR.glob("*.json"):
            if path.name == "en.json":
                continue
            with self.subTest(language=path.stem):
                language = _load_language(path)
                self.assertTrue(
                    FIXED_SASH_LABELS <= language["sashLabels"].keys(),
                    f"{path.name} is missing fixed sash translations",
                )

    def test_release_status_translation_uses_the_new_locale_entries(self):
        self.assertEqual(translate_sash("Cinema", "fr-FR"), "Au cinéma")
        self.assertEqual(translate_sash("Airing", "es-MX"), "En emisión")
        self.assertEqual(translate_sash("Physical", "pt-BR"), "Mídia Física")

    def test_festival_translation_uses_the_new_locale_entries(self):
        self.assertEqual(translate_sash("Golden Lion", "it-IT"), "Leone d'oro")
        self.assertEqual(translate_sash("Golden Bear", "es-ES"), "Oso de Oro")

    def test_the_weaker_festival_claim_is_translated_too(self):
        # Tier two carries as much of the poster as the top prize does, so a
        # missing translation here would render an English sash on a French one.
        self.assertEqual(translate_sash("Cannes Winner", "fr-FR"), "Primé à Cannes")
        self.assertEqual(translate_sash("Venice Winner", "it-IT"), "Premio Venezia")
        self.assertEqual(translate_sash("Sundance Winner", "pt-BR"), "Prêmio Sundance")

    def test_dropped_festivals_are_gone_from_every_locale(self):
        # Toronto, Busan, Rotterdam, SXSW and Tribeca named prizes we had no
        # way to verify.  A leftover entry is a sash waiting to come back.
        retired = {"People's Choice", "New Currents", "Tiger Award",
                   "SXSW Jury", "Tribeca AA"}
        for path in LANGUAGE_DIR.glob("*.json"):
            with self.subTest(language=path.stem):
                labels = _load_language(path)["sashLabels"].keys()
                self.assertFalse(retired & labels, f"{path.name} still lists a retired prize")


class FullVocabularyTests(unittest.TestCase):
    # Every shipped language is a complete copy of en.json: a gap renders as
    # English mid-poster, and a dropped {placeholder} loses the rank or date.
    PLACEHOLDER = re.compile(r"\{[a-z]+\}")

    def test_every_translation_carries_the_whole_vocabulary(self):
        english = _load_language(LANGUAGE_DIR / "en.json")
        for path in LANGUAGE_DIR.glob("*.json"):
            with self.subTest(language=path.stem):
                language = _load_language(path)
                for table in ("genreLabels", "sashLabels"):
                    self.assertEqual(language[table].keys(), english[table].keys(),
                                     f"{path.name} {table} differs from en.json")
                    for key, value in language[table].items():
                        self.assertEqual(sorted(self.PLACEHOLDER.findall(value)),
                                         sorted(self.PLACEHOLDER.findall(english[table][key])),
                                         f"{path.name} {key!r} changed its placeholders")

    def test_every_translation_renders_in_the_label_font(self):
        # Poster text is drawn in the label font the language resolves to
        # (Inter; Rubik for Hebrew, Arabic and Persian; Noto Sans Arabic for
        # Urdu), whichever font the user chose; no label font has CJK, Indic
        # or Thai glyphs — those would render as boxes.  Upper case is checked too: the landscape badge
        # uppercases its label.
        for path, choice in itertools.product(LANGUAGE_DIR.glob("*.json"), fonts.LABEL_FONTS):
            with self.subTest(language=path.stem, label_font=choice):
                language = _load_language(path)
                font = ImageFont.truetype(fonts.resolve_label_font(choice, language["code"]), 40,
                                          layout_engine=ImageFont.Layout.BASIC)
                notdef = bytes(font.getmask("\U0010FFFD"))
                text = "".join([*language["genreLabels"].values(),
                                *language["sashLabels"].values(),
                                *language["monthsShort"]])
                # Plus what the renderer puts between and before them.
                chars = set(text) | set(upper_label(text, language["code"])) | set("★·•…")
                # The joiner controls (Persian's zero-width non-joiner) need
                # no glyph, as fonts.covers has it.
                missing = sorted(c for c in chars
                                 if ord(c) > 127 and c not in fonts.JOINERS
                                 and bytes(font.getmask(c)) == notdef)
                self.assertFalse(missing, f"{path.name} has no glyph for {missing}")


class UpperLabelTests(unittest.TestCase):
    def test_turkish_keeps_the_dot_on_capital_i(self):
        self.assertEqual(upper_label("İptal edildi", "tr"), "İPTAL EDİLDİ")
        self.assertEqual(upper_label("Fransızca", "tr-TR"), "FRANSIZCA")

    def test_greek_drops_the_tonos_but_keeps_the_diaeresis(self):
        self.assertEqual(upper_label("Πρεμιέρα", "el"), "ΠΡΕΜΙΕΡΑ")
        self.assertEqual(upper_label("Εβραϊκά", "el"), "ΕΒΡΑΪΚΑ")

    def test_other_languages_match_str_upper(self):
        self.assertEqual(upper_label("Première", "fr"), "PREMIÈRE")
        self.assertEqual(upper_label("Italiano", None), "ITALIANO")
        self.assertEqual(upper_label("", "tr"), "")


if __name__ == "__main__":
    unittest.main()
