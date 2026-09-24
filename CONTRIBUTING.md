# Contributing

## Branches

**All pull requests target `dev`.**

`main` is release-only. It moves when a version ships, by a `dev` -> `main`
merge — never by a contributor PR. A PR opened against `main` is closed
automatically by the PR Base Guard workflow, with instructions for retargeting
it; your commits are untouched and changing the base is a two-click edit.

If you are working from a fork, branch from `dev` rather than committing to your
fork's `main`, and open the PR against `UmbraProjects:dev`.

## Adding a poster translation

Poster text (genre labels and info-sash labels) is translated per language in
`languages/<code>.json`. See the Poster Translations section of URL_REFERENCE.md for
the full rules. The two that trip people up:

- **Copy `languages/en.json` whole and translate only the values.** The keys are
  the exact canonical English strings the renderer emits; a missing key falls
  back to the English string, so a partial file renders half in English.
- **Region files are not diffs.** `pt-br.json` takes precedence over `pt.json`
  per *table*, not per *key*, so anything absent from a region file falls
  through to English rather than to the base language. A region file must carry
  the full vocabulary.

Contributed languages must be in Latin, Greek or Cyrillic script — the label
font (Inter) has no CJK, Arabic, Hebrew, Indic or Thai glyphs, and there is no
complex-text or right-to-left shaping. The i18n tests fail on any character the
font cannot draw, and on a file missing any key from `en.json`.

## Adding a setting

Every operator setting is declared once, in `config.py`, through
`settings.env()`:

```python
FOO_LIMIT = int(_env("FOO_LIMIT", "10", group="Caching", kind="int",
                     label="Foo limit", help="What it does and when to change it.",
                     min=0, max=100, advanced=True))
```

That one call reads the value (saved settings file, then environment, then the
default) *and* registers the field the admin dashboard renders, so there is
nothing else to wire up. `group` must be one of `settings.GROUP_ORDER`, `kind`
one of `settings.KINDS`, and `help` is required — a test checks all three.
Mark it `advanced=True` if it is a tuning knob nobody needs to run an
instance; those get a `### \`KEY\`` section in `ADVANCED.md`. Everything else
gets a `KEY=` line (with a comment) in `.env.example`. CONFIGURATION.md's settings
reference is generated — run `python3 tools/settings_docs.py --write` — and
`tests/test_settings_docs.py` fails if any of the three is out of step, or if
a module reads a setting from `os.environ` behind the registry's back.

## Before opening a PR

```bash
python3 -m pytest tests/ -q
```
