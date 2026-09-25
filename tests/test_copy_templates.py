"""Copy config copies the URL in the shape the chosen client can resolve.

Which placeholders a URL may use is a fact about the client that resolves them,
not a preference, so the button asks which client the URL is for rather than
asking the user to reason about placeholder syntax.  Left-click repeats the
last choice (and opens the menu when there is no choice yet), right-click
always opens the menu.
"""

from pathlib import Path
import re
import unittest


def _template_literal(html: str, template_id: str) -> str:
    """The COPY_TEMPLATES entry for one client, exactly as the page writes it."""
    start = html.index(f"{{ id: '{template_id}',")
    end = html.index("}", html.index("where:", start))
    return html[start : end + 1]


def _shape_of(html: str, template_id: str) -> str:
    """Which COPY_SHAPE_* constant that entry spreads."""
    return re.search(r"\.\.\.(COPY_SHAPE_\w+)", _template_literal(html, template_id)).group(1)


def _shape_flags(html: str, shape_name: str) -> dict:
    """The flags that shape sets, resolved from its declaration."""
    decl = re.search(rf"const {shape_name}\s*=\s*\{{(.*?)\}};", html, re.S).group(1)
    return {k: v == "true" for k, v in re.findall(r"(\w+):\s*(true|false)", decl)}


class CopyTemplateCatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_every_supported_client_has_an_entry(self):
        for template_id, name in (
            ("aiometadata", "AIOMetadata"),
            ("nuvio", "Nuvio"),
            ("xperience", "Xperience"),
            ("bingecat", "Bingecat"),
            ("discoverplus", "Discover+"),
        ):
            with self.subTest(client=template_id):
                self.assertIn(f"name: '{name}'", _template_literal(self.html, template_id))

    def test_there_are_only_two_shapes_behind_the_client_list(self):
        # Nuvio's resolver takes AIOMetadata's placeholder set, optional
        # "{name?}" form included, and Xperience builds Nuvio configurations,
        # so all three carry the same ids. Bingecat and Discover+ are the
        # holdouts that reject "{name?}" at config time.
        for client in ("aiometadata", "nuvio", "xperience"):
            with self.subTest(client=client):
                self.assertEqual(_shape_of(self.html, client), "COPY_SHAPE_OPTIMAL")
        for client in ("bingecat", "discoverplus"):
            with self.subTest(client=client):
                self.assertEqual(_shape_of(self.html, client), "COPY_SHAPE_REQUIRED")

    def test_the_silent_failure_is_written_down(self):
        # Tested against both: Bingecat rejects a "{name?}" URL at config time
        # and says so, Discover+ accepts it and then serves nothing with no
        # indication why. That second one is why the shape is decided from the
        # client rather than left to the user, and it is not discoverable from
        # anything else in this file.
        self.assertIn("Discover+ accepts it, saves it, and then silently", self.html)

    def test_the_two_shapes_are_all_on_and_all_off(self):
        # The flags describe one fact — whether the client implements "{name?}"
        # — so they never travel apart. When a holdout gains the form, moving
        # its entry onto COPY_SHAPE_OPTIMAL is the whole change.
        self.assertEqual(
            _shape_flags(self.html, "COPY_SHAPE_OPTIMAL"),
            {"tmdbOptional": True, "imdbOptional": True, "animeIds": True},
        )
        self.assertEqual(
            _shape_flags(self.html, "COPY_SHAPE_REQUIRED"),
            {"tmdbOptional": False, "imdbOptional": False, "animeIds": False},
        )

    def test_every_entry_names_where_the_url_goes(self):
        # Surfaced on the button's tooltip after a choice, and in the
        # manual-copy fallback on a non-secure origin.
        for template_id in ("aiometadata", "nuvio", "bingecat"):
            with self.subTest(client=template_id):
                self.assertIn("where:", _template_literal(self.html, template_id))

    def test_only_nuvio_gets_the_keys_as_literals(self):
        # Nuvio has no "{tmdb_key}" / "{mdblist_key}" to substitute, so it
        # would send the placeholder verbatim and the server would try it as a
        # key. The clients that do fill them keep the placeholder.
        self.assertIn("...COPY_KEYS_LITERAL", _template_literal(self.html, "nuvio"))
        for client in ("aiometadata", "xperience", "bingecat", "discoverplus"):
            with self.subTest(client=client):
                self.assertNotIn("COPY_KEYS_LITERAL", _template_literal(self.html, client))
        self.assertIn("const COPY_KEYS_LITERAL = { literalKeys: true };", self.html)
        self.assertIn(
            "const keyHolders = usePlaceholders && !template.literalKeys;", self.html
        )

    def test_keys_go_optional_wherever_the_ids_do(self):
        # A key typed into the configurator says nothing about whether the
        # client holds one. AIOMetadata abandons the whole URL on a required
        # placeholder it cannot fill, so a user with no key there lost every
        # poster; in the optional form the server's own key is used instead.
        # Clients that reject "{name?}" keep the required form.
        self.assertIn(
            "const keyHolder  = name => template.tmdbOptional ? `{${name}?}` : `{${name}}`;",
            self.html,
        )
        self.assertIn("keyHolders ? keyHolder('tmdb_key')    : userTmdbKey", self.html)
        self.assertIn("keyHolders ? keyHolder('mdblist_key') : userMdblistKey", self.html)

    def test_the_saved_configuration_carries_no_client_choice(self):
        # saveSettings round-trips through buildBaseParams with no templateId,
        # which must land on the neutral shape — otherwise a remembered client
        # would leak into stored settings and into every exported URL.
        self.assertIn("templateId = null } = {}", self.html)
        self.assertIn(
            "const template       = COPY_TEMPLATES.find(t => t.id === templateId) "
            "|| COPY_TEMPLATE_NEUTRAL;",
            self.html,
        )
        self.assertIn("const COPY_TEMPLATE_NEUTRAL = { id: '', name: '',", self.html)
        # Every flag off: the neutral shape is tmdb_id in its plain form and
        # nothing else, which is what a concrete preview URL wants too.
        self.assertIn(
            "tmdbOptional: false, imdbOptional: false, animeIds: false };", self.html
        )


class CopyButtonBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_left_click_copies_the_remembered_client(self):
        self.assertIn("async function copyUrl() {", self.html)
        self.assertRegex(
            self.html,
            r"const current = rememberedTemplate\(\);\s*"
            r"if \(!current\) \{ openCopyMenu\(\); return; \}\s*"
            r"await copyTemplate\(current\.id\);",
        )

    def test_right_click_always_opens_the_menu(self):
        self.assertIn('oncontextmenu="return openCopyMenu(event)"', self.html)
        self.assertIn("ev?.preventDefault();", self.html)

    def test_touch_gets_a_way_back_into_the_menu(self):
        # There is no right-click on a phone, so press-and-hold opens it too.
        # Mouse pointers are skipped — they have the real thing.
        self.assertIn("btn.addEventListener('pointerdown', armCopyLongPress);", self.html)
        self.assertIn("if (ev.pointerType === 'mouse') return;", self.html)
        # Android delivers contextmenu for the same gesture that already
        # tripped the timer; the menu must not close on that second event.
        self.assertIn("if (Date.now() - _copyMenuOpenedAt < 600) return false;", self.html)
        # And the click that ends the press must not copy on top of it.
        self.assertIn(
            "if (_copyLongPressed) { _copyLongPressed = false; return; }", self.html
        )

    def test_the_choice_survives_a_reload(self):
        self.assertIn("const COPY_TEMPLATE_KEY = 'postersplus_copy_template';", self.html)
        self.assertIn("localStorage.setItem(COPY_TEMPLATE_KEY, template.id);", self.html)
        self.assertIn("id = localStorage.getItem(COPY_TEMPLATE_KEY);", self.html)

    def test_storage_being_unavailable_is_not_fatal(self):
        # Private windows and blocked site data throw on access; the button
        # must still copy, just without remembering.
        self.assertRegex(
            self.html, r"try \{ localStorage\.setItem\(COPY_TEMPLATE_KEY, template\.id\); \} catch"
        )
        self.assertRegex(
            self.html, r"try \{ id = localStorage\.getItem\(COPY_TEMPLATE_KEY\); \} catch"
        )

    def test_the_url_box_follows_the_copied_shape(self):
        # It is the manual-copy fallback when the clipboard refuses, so it must
        # not still be showing the previous client's URL.
        self.assertIn(
            "if (_SHOW_URL_BOX) document.getElementById('url-display-input').value = url;",
            self.html,
        )

    def test_the_menu_marks_what_a_left_click_would_copy(self):
        self.assertIn("classList.toggle('is-current'", self.html)
        self.assertIn(".menu-item.is-current", self.html)

    def test_the_menu_is_built_from_the_catalogue(self):
        # One source of truth: adding a client must not mean editing markup.
        self.assertIn('<div class="menu" id="copy-menu"', self.html)
        self.assertRegex(self.html, r"for \(const t of COPY_TEMPLATES\) \{[^}]*createElement\('button'\)")

    def test_both_menus_share_dismissal(self):
        # Escape, an outside click, a scroll or a resize closes either one.
        self.assertIn(
            "function closeMenus() { closeExternalMenu(); closeCopyMenu(); }", self.html
        )
        self.assertIn("window.addEventListener('scroll', closeMenus, true);", self.html)
        self.assertIn(
            "'#external-menu, #external-link, #copy-menu, #copy-config-btn'", self.html
        )


if __name__ == "__main__":
    unittest.main()
