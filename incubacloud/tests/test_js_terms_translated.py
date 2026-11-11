"""Every string the panel's scripts translate reaches the browser in Spanish.

The browser gets its translations from the ``odoo-javascript`` entries of
each module's ``es.po``, and from nothing else. A term the scripts pass to
``_t`` that the file holds only as a model field or a selection (the same
words, another source) has no such entry: the .pot and the .po then look
complete, since the words are there, and the screen stays in English. The
check reads the payload the way the server builds it and asks for each term.
"""
import pathlib
import re

from odoo.tests.common import BaseCase
from odoo.tools.translate import code_translations

#: The modules of this repository that draw in the panel's bundle.
MODULES = ("incubacloud", "incubacloud_mcp", "incubacloud_assistant")

#: A ``_t("...")`` or ``_t('...')`` literal and what follows it, so a string
#: continued by ``+`` or by another literal (one sentence cut in lines, which
#: the extraction cannot see whole) is left out.
_T_LITERAL = re.compile(
    r"""\b_t\(\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')(\s*(?:\+|"|'))?"""
)


def script_terms(directory):
    """Terms the scripts under ``directory`` pass to ``_t`` whole.

    :param pathlib.Path directory: a module's ``static/src``
    :return: ``{term: [file names]}``, unescaped
    :rtype: dict
    """
    found = {}
    for path in sorted(directory.rglob("*.js")):
        for match in _T_LITERAL.finditer(path.read_text()):
            if match.group(3):
                continue
            raw = match.group(1) if match.group(1) is not None else match.group(2)
            term = raw.replace('\\"', '"').replace("\\'", "'")
            if term.strip():
                found.setdefault(term, []).append(path.name)
    return found


def untranslated_terms(module, root, lang="es"):
    """Terms of ``module``'s scripts the browser would get without ``lang``.

    :param str module: addon name
    :param pathlib.Path root: the folder holding the addons
    :param str lang: language code
    :return: ``{term: [file names]}`` of the terms missing from the payload
    :rtype: dict
    """
    served = {
        message["id"]
        for message in code_translations.get_web_translations(module, lang)["messages"]
    }
    directory = root / module / "static" / "src"
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    terms = script_terms(directory)
    if not terms:
        raise AssertionError("no term read in %s" % directory)
    return {term: files for term, files in terms.items() if term not in served}


class TestJsTermsTranslated(BaseCase):

    def test_scripts_terms_reach_the_browser_in_spanish(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        for module in MODULES:
            with self.subTest(module=module):
                missing = untranslated_terms(module, root)
                self.assertFalse(
                    missing,
                    "Terms of %s's scripts missing from the Spanish payload "
                    "(no entry with the odoo-javascript flag and a "
                    "translation; one that exists only as a field or a "
                    "selection does not count, give it the script's "
                    "reference too): %s" % (module, sorted(missing)),
                )

    def test_settings_declarations_are_found(self):
        """The reading finds the declarations, so an empty list is no pass."""
        root = pathlib.Path(__file__).resolve().parents[2]
        terms = script_terms(root / "incubacloud" / "static" / "src")
        for term in ("Logs & retention", "Network edge", "1 field needs attention"):
            self.assertIn(term, terms)
