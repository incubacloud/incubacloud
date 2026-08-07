"""Structural guards for the two regressions the light-theme sweep fixed.

An OWL component prop written as a quoted expression
(``placeholder="'Search…'"``) never reaches the i18n extractor: the
string is absent from the .pot, so no .po can ever translate it and a
Spanish user meets an English placeholder inside an otherwise translated
screen. The working form is the ``.translate`` attribute suffix. Every
SearchSelect placeholder shipped the broken way until 1.0.36.

Likewise, a stylesheet that paints with literal colors instead of the
``--rl-*`` tokens looks right in the dark theme and falls apart under
``data-ic-theme="light"`` — the shared modal chrome, the import dialog
and the instance-detail panels all rotted that way, invisibly, because
nothing failed. Nothing in the Python suite renders a pixel, so both
checks are textual, like the shared-helper import guard.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_SRC = pathlib.Path(__file__).resolve().parent.parent / "static" / "src"

# Component props whose value the user reads on screen.
_TRANSLATABLE_PROPS = ("placeholder", "emptyLabel")

# Quoted-expression literals that are code, not copy: branch names and
# the addons.yaml wildcard labels. Extend consciously or use .translate.
_PROP_LITERAL_ALLOWLIST = {"", "main", "ALL", "NONE"}

_PROP_LITERAL_RE = re.compile(
    r"\b({props})=\"'([^']*)'\"".format(props="|".join(_TRANSLATABLE_PROPS))
)

# Token definition files — the only places a raw color belongs.
_TOKEN_FILES = {"scss/relay.scss", "scss/variables.scss"}

# Theme-independent literals: black scrims/shadows and the modal scrim.
_SCRIM_OR_SHADOW_RE = re.compile(r"rgba\(\s*(?:0\s*,\s*0\s*,\s*0|6\s*,\s*10\s*,\s*8)")

# Deliberate survivors, one (file, literal) pair each. The violet sync
# tag has no token on purpose: it reads fine in both themes.
_COLOR_ALLOWLIST = {
    ("components/instance_detail/instance_detail.scss", "#8b5cf6"),
}

_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d")


def _prop_literal_offenders():
    """Yield translatable component props passed as quoted literals.

    :return: iterator of (path, prop, literal) outside the allowlist.
    """
    for path in sorted(_SRC.rglob("*.xml")):
        for prop, literal in _PROP_LITERAL_RE.findall(path.read_text()):
            if literal not in _PROP_LITERAL_ALLOWLIST:
                yield path, prop, literal


def _color_offenders():
    """Yield raw color literals in component stylesheets.

    :return: iterator of (path, literal) that must use tokens instead.
    """
    for path in sorted(_SRC.rglob("*.scss")):
        rel = str(path.relative_to(_SRC))
        if rel in _TOKEN_FILES:
            continue
        for line in path.read_text().splitlines():
            for literal in _COLOR_RE.findall(line):
                if _SCRIM_OR_SHADOW_RE.search(line):
                    continue
                if (rel, literal.rstrip("(").strip()) in _COLOR_ALLOWLIST:
                    continue
                key = (rel, literal if literal.startswith("#") else line.strip())
                yield key


class TestSpaThemeI18nGuards(BaseCase):

    def test_translatable_props_are_not_quoted_literals(self):
        """placeholder/emptyLabel must use .translate, not "'…'"."""
        offenders = [
            f"{path.relative_to(_SRC)}: {prop}=\"'{literal}'\""
            for path, prop, literal in _prop_literal_offenders()
        ]
        self.assertFalse(
            offenders,
            "untranslatable component props (use prop.translate=\"…\"):\n  "
            + "\n  ".join(offenders),
        )

    def test_component_styles_use_theme_tokens(self):
        """No component stylesheet may hardcode a color literal."""
        offenders = sorted({f"{rel}: {what}" for rel, what in _color_offenders()})
        self.assertFalse(
            offenders,
            "raw colors outside the token files (use var(--rl-…)):\n  "
            + "\n  ".join(offenders),
        )

    def test_the_scans_see_the_tree(self):
        """Canary: silent empty scans would make both guards vacuous."""
        xml_with_translate = sum(
            1 for p in _SRC.rglob("*.xml") if ".translate=" in p.read_text()
        )
        self.assertGreaterEqual(
            xml_with_translate, 8,
            "the template scan stopped seeing .translate props",
        )
        self.assertGreaterEqual(
            sum(1 for _ in _SRC.rglob("*.scss")), 20,
            "the stylesheet scan stopped seeing scss files",
        )
