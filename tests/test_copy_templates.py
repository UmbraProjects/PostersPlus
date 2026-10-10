"""Copy config copies the URL in the shape the chosen client can resolve.

Which placeholders a URL may use is a fact about the client that resolves them,
not a preference, so the button asks which client the URL is for rather than
asking the user to reason about placeholder syntax.  Every client but
Discover+ takes the same URL, so left-click copies that one unless Discover+
was the last pick, and right-click (or a long press) opens the menu.
"""

from pathlib import Path
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
import unittest
from unittest import mock


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
            ("standard", "Default"),
            ("discoverplus", "Discover+"),
        ):
            with self.subTest(client=template_id):
                self.assertIn(f"name: '{name}'", _template_literal(self.html, template_id))

    def test_there_are_only_two_shapes_behind_the_client_list(self):
        # AIOMetadata, Nuvio, Bingecat and Xperience all resolve Nuvio's
        # placeholder set, optional "{name?}" form included. Discover+ is the
        # holdout.
        self.assertEqual(_shape_of(self.html, "standard"), "COPY_SHAPE_OPTIMAL")
        self.assertEqual(_shape_of(self.html, "discoverplus"), "COPY_SHAPE_REQUIRED")
        menu = self.html[self.html.index("const COPY_TEMPLATES = ["):]
        menu = menu[: menu.index("];")]
        self.assertEqual(re.findall(r"\{ id: '(\w+)'", menu), ["standard", "discoverplus"])

    def test_choices_from_before_the_merge_land_on_the_shared_url(self):
        decl = re.search(r"const COPY_TEMPLATE_LEGACY = \{(.*?)\};", self.html, re.S).group(1)
        for old in ("aiometadata", "nuvio", "xperience", "bingecat"):
            with self.subTest(client=old):
                self.assertIn(f"{old}: 'standard'", decl)
        self.assertIn("id = COPY_TEMPLATE_LEGACY[id] || id;", self.html)

    def test_the_silent_failure_is_written_down(self):
        # Discover+ accepts a "{name?}" URL and then serves nothing with no
        # indication why. That is why the shape is decided from the client
        # rather than left to the user, and it is not discoverable from
        # anything else in this file.
        self.assertIn("Discover+ accepts a \"{name?}\" URL, saves it, and then silently", self.html)

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
        for template_id in ("standard", "discoverplus"):
            with self.subTest(client=template_id):
                self.assertIn("where:", _template_literal(self.html, template_id))

    def test_the_shared_url_gets_the_keys_as_literals(self):
        # Nuvio has no "{tmdb_key}" / "{mdblist_key}" to substitute, so it
        # would send the placeholder verbatim and the server would try it as a
        # key; the clients sharing its URL get the literals too. Discover+
        # fills them, so it keeps the placeholder.
        self.assertIn("...COPY_KEYS_LITERAL", _template_literal(self.html, "standard"))
        self.assertNotIn("COPY_KEYS_LITERAL", _template_literal(self.html, "discoverplus"))
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
            "const template       = [...COPY_TEMPLATES, COPY_TEMPLATE_SHARE].find(t => t.id === templateId)\n"
            "                         || COPY_TEMPLATE_NEUTRAL;",
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

    def test_left_click_copies_the_shared_url_unless_discover_was_picked(self):
        # Defaulting rather than opening the menu first is what stops an
        # AIOMetadata URL being pasted into Nuvio and the like.
        self.assertIn("async function copyUrl() {", self.html)
        self.assertIn("await copyTemplate(rememberedTemplate().id);", self.html)
        self.assertIn(
            "return COPY_TEMPLATES.find(t => t.id === id) || COPY_TEMPLATES[0];", self.html
        )
        self.assertIn("const COPY_TEMPLATES = [\n  { id: 'standard',", self.html)

    def test_the_tooltip_names_both_routes(self):
        self.assertIn(
            "Left-click to copy the URL for AIOMetadata, Nuvio, Bingecat and Xperience", self.html
        )
        self.assertIn(
            "Right-click or long press to copy the config for Discover+ or share your config securely.",
            self.html,
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
            "function closeMenus() { closeExternalMenu(); closeCopyMenu(); closeResetMenu(); }", self.html
        )
        self.assertIn("window.addEventListener('scroll', closeMenus, true);", self.html)
        self.assertIn(
            "'#external-menu, #external-link, #copy-menu, #copy-config-btn'", self.html
        )



class ShareSettingsTests(unittest.TestCase):
    """Share settings must not expose private instances or account keys."""

    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def run_share_js(self, body, extra=()):
        """Use Node's parser to extract complete declarations, including nested
        blocks, comments, regexes and template literals; no comment boundaries."""
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required: install Node.js and put node on PATH")

        class Scripts(HTMLParser):
            inside = False
            source = ""

            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    self.inside = True

            def handle_endtag(self, tag):
                if tag == "script":
                    self.inside = False
                    self.source += "\n"

            def handle_data(self, data):
                if self.inside:
                    self.source += data

        scripts = Scripts()
        scripts.feed(self.html)
        names = ["SHARE_ORIGIN", "SHARE_LEGACY_ORIGIN", "_SHARE_DROP", "shareOrigin",
                 "buildShareUrl", "isShareUrl", "applyImport", "decodePlaceholders", *extra]
        script = "const source = " + json.dumps(scripts.source) + ";\n"
        script += r"""
            const assert = require('node:assert/strict');
            const vm = require('node:vm');
            new vm.Script(source); // Also syntax-check the entire page.
            function declaration(name) {
              const match = new RegExp('^(?:(?:async )?function |const |let )'
                + name + '\\b', 'm').exec(source);
              assert.ok(match, 'Missing declaration: ' + name);
              const tail = source.slice(match.index);
              const terminator = /^(?:async )?function /.test(tail) ? '}' : ';';
              for (let end = tail.indexOf(terminator); end >= 0;
                   end = tail.indexOf(terminator, end + 1)) {
                const candidate = tail.slice(0, end + 1);
                try { new vm.Script(candidate); } catch (error) {
                  if (error instanceof SyntaxError) continue;
                  throw error;
                }
                return candidate;
              }
              throw new Error('Incomplete declaration: ' + name);
            }
        """
        script += "eval(" + json.dumps(names) + ".map(declaration).join('\\n') + " + json.dumps(body) + ");"
        result = subprocess.run([node, "-"], input=script, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_node_skips_javascript_tests(self):
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(unittest.SkipTest, "install Node.js"):
                self.run_share_js("")

    def test_only_explicit_public_origins_are_preserved(self):
        public = ["https://postersplus.cc", "https://postersplus.slokker.cc",
                  "https://postersplus.stremio.ru", "https://postersplus.elfhosted.com",
                  "https://new-hoster.example", "https://new-hoster.example:8443"]
        cases = [(host, host) for host in public]
        cases += [(host + "/", host) for host in public]
        cases += [("HTTPS://NEW-HOSTER.EXAMPLE:443/", "https://new-hoster.example")]
        invalid = [None, "", "not a URL", "postersplus.cc", "//postersplus.cc",
                   "https://", "https://[broken", "https://example.com:bad", "null",
                   "blob:https://example.com/id", "file:///example.com", "http://example.com",
                   "https://user:secret@example.com", "https://example.com/private",
                   "https://example.com/?key=secret", "https://example.com/#private"]
        cases += [(host, public[0]) for host in invalid]
        self.run_share_js("const cases = " + json.dumps(cases) + ";\n" + r"""
            let serverCaps = {};
            function vEl() { throw new Error('Must not read the browser/private domain'); }
            function buildBaseParams(options) {
              assert.equal(options.domainOverride, shareOrigin());
              assert.equal(options.usePlaceholders, true);
              assert.equal(options.templateId, 'share');
              return options.domainOverride + '/poster?shape={shape}&rating_display_mode=2'
                + '&tmdb_id={tmdb_id?}&imdb_id={imdb_id?}&stremio_id={id}&type={type}';
            }
            // Before capabilities load, even a known public host stays private.
            assert.equal(shareOrigin(), 'https://postersplus.cc');
            for (const [input, expected] of cases) {
              serverCaps = {public_share_origin: input};
              const output = new URL(buildShareUrl());
              assert.equal(output.origin, expected, String(input));
              assert.equal(output.pathname, '/poster');
              assert.equal(output.username, '');
              assert.equal(output.password, '');
              assert.equal(output.hash, '');
              assert.equal(output.searchParams.get('share'), '1');
              assert.equal(output.searchParams.get('shape'), '{shape}');
              assert.equal(output.searchParams.get('rating_display_mode'), '2');
              assert.equal(output.searchParams.get('tmdb_id'), '{tmdb_id?}');
              assert.equal(output.searchParams.get('imdb_id'), '{imdb_id?}');
              assert.equal(output.searchParams.get('stremio_id'), '{id}');
              assert.equal(output.searchParams.get('type'), '{type}');
              assert.equal(isShareUrl(output.href), true);
            }
        """)

    def test_share_generation_filters_sensitive_parameters(self):
        self.run_share_js(r"""
            const serverCaps = {};
            function buildBaseParams() {
              const p = new URLSearchParams({
                tmdb_id: '123', imdb_id: 'tt123', stremio_id: 'kitsu:123', type: 'movie',
                access_key: 'private', tmdb_key: 'secret', mdblist_key: 'secret',
                logo_language: 'fr', primary_client: 'nuvio', resolution: '2000',
                shape: '{shape}', rating_mode: '2', landscape_rating_mode: '3',
                sash_priority: 'default,awards', share: '0'
              });
              p.append('access_key', 'another-secret');
              return 'https://private.example/poster?' + p;
            }
            function getSashPriorityParam(options) {
              assert.deepEqual(options, {compact: false});
              return 'awards,imdb';
            }
            assert.equal(buildShareUrl(), 'https://postersplus.cc/poster?shape={shape}'
              + '&rating_mode=2&landscape_rating_mode=3&sash_priority=awards%2Cimdb&share=1');
        """)

    def test_shared_and_normal_urls_take_the_correct_import_path(self):
        self.run_share_js(r"""
            let raw, imported, remembered, closed;
            const document = {getElementById: () => ({value: raw})};
            function importUrl(url, options) { imported = {url, options}; return true; }
            function _rememberImport(url) { remembered = url; }
            function closeImportModal() { closed = true; }
            const cases = [];
            for (const origin of ['https://postersplus.cc', 'https://new-hoster.example']) {
              cases.push([origin + '/poster?share=1&rating_mode=2', true]);
              // The marker requests a stricter import, even if someone adds
              // concrete identity or credentials to a link from an unknown host.
              cases.push([origin + '/poster?share=1&tmdb_id=123&tmdb_key=incoming', true]);
              // Templates and concrete titles both remain normal poster imports.
              for (const query of ['tmdb_id=123&type=movie', 'shape={shape}',
                                   'rating_mode=2', 'share=0', 'share=true', 'share=',
                                   'tmdb_id=123#share=1']) {
                cases.push([origin + '/poster?' + query, false]);
              }
              cases.push([origin + '.other.test/poster?share=1', true]);
              cases.push([origin + ':8443/poster?share=1', true]);
              cases.push(['blob:' + origin + '/poster?share=1', false]);
            }
            cases.push(['https://share.postersplus.invalid/poster?shape={shape}', true],
                       ['https://share.postersplus.invalid/poster?share=0', true],
                       ['https://share.postersplus.invalid.evil.test/poster', false],
                       ['http://share.postersplus.invalid/poster', false],
                       ['blob:https://share.postersplus.invalid/poster', false],
                       ['https://private.example/poster?tmdb_id=123', false],
                       ['not a URL', false]);
            for (const [url, share] of cases) {
              assert.equal(isShareUrl(url), share, url);
              raw = '  ' + url + '  ';
              closed = false;
              applyImport();
              assert.deepEqual(imported, {url, options: share
                ? {settingsOnly: true, share: true} : {}}, url);
              assert.equal(remembered, url);
              assert.equal(closed, true);
            }
        """)

    def test_share_marker_requires_a_poster_endpoint(self):
        self.run_share_js(r"""
            for (const origin of ['https://new-hoster.example:8443',
                                  'https://share.postersplus.invalid']) {
              for (const path of ['/poster', '/poster/', '/proxy/poster', '/proxy/poster/']) {
                assert.equal(isShareUrl(origin + path + '?share=1'), true, path);
                // Unmarked public posters stay normal imports; legacy shares
                // remain recognizable without a marker.
                assert.equal(isShareUrl(origin + path + '?shape={shape}'),
                  origin === 'https://share.postersplus.invalid', path);
              }
              for (const path of ['/', '/configurator', '/poster.jpg', '/poster/extra',
                                  '/notposter', '/poster%2F']) {
                assert.equal(isShareUrl(origin + path + '?share=1'), false, path);
                assert.equal(isShareUrl(origin + path), false, path);
              }
            }
        """)

    def test_keys_ids_and_personal_choices_are_dropped(self):
        start = self.html.index("const _SHARE_DROP")
        drop = self.html[start:self.html.index("];", start)]
        for key in ("tmdb_key", "mdblist_key", "access_key", "tmdb_id", "imdb_id",
                    "stremio_id", "type", "logo_language", "primary_client", "resolution"):
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", drop)

    def test_last_import_drops_marker_and_sensitive_parameters(self):
        self.run_share_js(r"""
            let saved = [];
            function _readUserPresets() { return saved; }
            function _writeUserPresets(value) { saved = value; }
            _rememberImport('https://public.example/poster?share=1&share=0&SHARE=1'
              + '&tmdb_id={tmdb_id?}&type={type}&access_key=secret&tmdb_key=secret'
              + '&shape={shape}&rating_display_mode=2');
            assert.equal(saved.length, 1);
            assert.equal(saved[0].name, 'Last imported URL');
            assert.equal(saved[0].params, 'shape=%7Bshape%7D&rating_display_mode=2');
            // Reimporting a normal URL still replaces the same preset.
            _rememberImport('https://private.example/poster?tmdb_id=123&rating_display_mode=3');
            assert.equal(saved.length, 1);
            assert.equal(saved[0].params, 'rating_display_mode=3');
        """, extra=("_PRESET_DROPPED", "_CORE_PARAMS", "LAST_IMPORT_ID",
                    "_importThumbPending", "_presetParams", "_rememberImport"))

    def test_public_origin_is_explicit_operator_configuration(self):
        from fastapi.testclient import TestClient
        import main
        import settings

        self.assertEqual(settings.REGISTRY['PUBLIC_SHARE_ORIGIN'].default, '')
        client = TestClient(main.app)
        for origin in ('', 'https://public.example'):
            with self.subTest(origin=origin), \
                 mock.patch.object(main._cfg, 'ACCESS_KEY', None), \
                 mock.patch.object(main._cfg, 'PUBLIC_SHARE_ORIGIN', origin), \
                 mock.patch.object(main._cfg, 'PUBLIC_URL', 'https://private.example'), \
                 mock.patch.object(main.presets, 'public_list', return_value=[]), \
                 mock.patch.object(main.custom_fonts, 'public_list', return_value=[]):
                response = client.get('/server-caps', headers={
                    'Host': 'private.lan', 'X-Forwarded-Host': 'forged.example',
                    'X-Forwarded-Proto': 'https',
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['public_share_origin'], origin)

    def test_both_shapes_travel_with_defaults_left_out(self):
        # Short enough to paste in a chat message: what is at its default is
        # left out, as on any copied URL, and filled back in on import.
        self.assertIn("COPY_TEMPLATE_SHARE = { id: 'share', name: 'Share settings', "
                      "...COPY_SHAPE_OPTIMAL, ...COPY_SHAPE_DUAL };", self.html)
        self.assertIn("buildBaseParams({ usePlaceholders: true, templateId: 'share',", self.html)

    def test_only_dropped_defaults_depend_on_the_instance(self):
        # A share leaves defaults out and the importer fills them from its own
        # server, so a default an operator setting moves would import as the
        # recipient's value, not the sender's.  The only one is the logo
        # language, which a share drops anyway.  A setting that starts
        # moving a render default must be added to _SHARE_DROP or kept in
        # the share.
        import main
        import settings as _settings
        base = (main._render_param_defaults(), main._render_param_defaults("landscape"))
        moved = set()
        for key, setting in _settings.REGISTRY.items():
            current = getattr(main._cfg, key, None)
            if isinstance(current, bool):
                other = not current
            elif isinstance(current, (int, float)):
                other = current + 1
            elif isinstance(current, str):
                other = current + "x" if current else "x"
            else:
                continue
            with mock.patch.object(main._cfg, key, other):
                try:
                    now = (main._render_param_defaults(), main._render_param_defaults("landscape"))
                except Exception:
                    continue
            for b, n in zip(base, now):
                moved |= {k for k in b.keys() | n.keys() if b.get(k) != n.get(k)}
        start = self.html.index("const _SHARE_DROP")
        drop = self.html[start:self.html.index("];", start)]
        self.assertEqual({k for k in moved if f"'{k}'" not in drop}, set())

    def test_import_takes_settings_only(self):
        self.assertIn("isShareUrl(raw) ? { settingsOnly: true, share: true } : {}", self.html)


if __name__ == "__main__":
    unittest.main()
