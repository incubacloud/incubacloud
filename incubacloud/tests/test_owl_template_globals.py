"""No OWL template calls a JavaScript global that OWL does not give it.

OWL compiles a template's expressions against the component: every name
that is not in its short list of reserved words (``Math``, ``Array``,
``Object``, ``Date``, ``RegExp``, ``console``, ``window`` and a few more)
is read as ``ctx.<name>``. ``Boolean(x)`` becomes ``ctx.Boolean(x)`` and
the page stops with ``TypeError: ctx.Boolean is not a function`` the
moment the element renders. An SFTP destination's Password field (1.0.238)
rendered one and never showed (found by the QA of 26-nov-2026). ``!!x`` is
the template's way to say ``Boolean(x)``.

The check reads the templates' expressions, with their string literals
taken out (but the ``${...}`` of a template literal, which OWL compiles),
for the globals a template is likely to reach for.
"""
import pathlib
import re
from xml.etree import ElementTree

from odoo.tests.common import BaseCase

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent

# JavaScript globals OWL does not expose to a template (its
# RESERVED_WORDS in web/static/lib/owl/owl.js lists the ones it does).
_UNREACHABLE = (
    "Boolean", "Number", "String", "JSON", "parseInt", "parseFloat",
    "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent",
    "Intl", "Set", "Map", "Promise", "Symbol", "BigInt", "encodeURI",
    "URL", "URLSearchParams", "Error", "structuredClone",
)
_CALL = re.compile(r"(?<![\w$.])(%s)\b" % "|".join(_UNREACHABLE))
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_TEMPLATE_LITERAL = re.compile(r"`(?:[^`\\]|\\.)*`")
_PLACEHOLDER = re.compile(r"\$\{(.*?)\}")
_INTERPOLATION = re.compile(r"\{\{(.*?)\}\}|#\{(.*?)\}")

# Directives whose value is a name, not an expression.
_NAMES = {
    "t-name", "t-inherit", "t-inherit-mode", "t-as", "t-set", "t-ref",
    "t-slot", "t-set-slot", "t-call", "t-translation",
    "t-translation-context",
}


def _code(expression):
    """Take an expression's string literals out, keeping what OWL compiles.

    :param expression: a template expression
    :return: the expression with each quoted string replaced by ``''`` and
        each template literal by the ``${...}`` pieces it holds
    """
    expression = _TEMPLATE_LITERAL.sub(
        lambda match: " ".join(_PLACEHOLDER.findall(match.group(0))) or "''",
        expression,
    )
    return _STRING.sub("''", expression)


def _expressions(element):
    """Yield the expressions OWL compiles from one element's attributes.

    :param element: an element of a template file
    :return: iterator of expression strings: every ``t-`` directive but
        the named ones, the pieces of a ``t-attf-`` format, every
        attribute of a component (a tag that starts with a capital, or one
        with ``t-component``), whose props are expressions but for the
        ``.translate`` ones, and the text of an inheriting view's
        ``<attribute>`` that sets a directive
    """
    if element.tag == "attribute":
        name = element.get("name", "")
        if name.startswith("t-") and name not in _NAMES and element.text:
            yield element.text
        return
    component = element.tag[:1].isupper() or "t-component" in element.attrib
    for name, value in element.attrib.items():
        if name.endswith(".translate") or name.startswith("t-translation-context"):
            continue
        if name.startswith("t-attf-"):
            for match in _INTERPOLATION.finditer(value):
                yield match.group(1) or match.group(2)
        elif name.startswith("t-") and name not in _NAMES:
            yield value
        elif component and not name.startswith("t-"):
            yield value


def owl_globals_in(root):
    """List the unreachable globals the OWL templates under a directory call.

    :param root: a repository whose modules keep their templates in
        ``<module>/static/src``
    :return: sorted ``"<file>:<global> in <expression>"`` lines, one per
        offending expression
    """
    root = pathlib.Path(root)
    found = []
    for path in sorted(root.glob("*/static/src/**/*.xml")):
        for element in ElementTree.parse(path).iter():  # nosec B314 — this repo's own templates
            for expression in _expressions(element):
                for name in _CALL.findall(_code(expression)):
                    found.append(
                        "%s: %s in %s" % (path.relative_to(root), name, expression.strip())
                    )
    return found


class TestOwlTemplateGlobals(BaseCase):

    def test_the_templates_call_no_global_owl_hides(self):
        """No template of the repository's modules calls one."""
        found = owl_globals_in(_REPO)
        self.assertFalse(
            found,
            "these template expressions call a global OWL reads as ctx.<name>, "
            "which stops the page when it renders (write !!x for Boolean(x)):\n%s"
            % "\n".join(found),
        )

    def test_the_check_sees_a_call_in_a_prop_and_a_directive(self):
        """The check must catch what it is for, and leave strings and members alone."""
        template = ElementTree.fromstring(  # nosec B314 — a literal of this test
            '<t t-name="x">'
            '<Comp flag="Boolean(a)" label="\'Number(1)\'" n="this.String(b)"'
            ' title.translate="Set up backups"/>'
            '<div t-if="JSON.stringify(c)" t-attf-class="k-{{ Number(d) }}" class="Boolean(e)"/>'
            '<t t-component="C" header="Map(f)"/>'
            '<span t-att-title="`${String(g)} Set`"/>'
            '<attribute name="t-if">Promise(h)</attribute>'
            '<attribute name="class">Error</attribute>'
            "</t>"
        )
        calls = [
            name
            for element in template.iter()
            for expression in _expressions(element)
            for name in _CALL.findall(_code(expression))
        ]
        self.assertEqual(calls, ["Boolean", "JSON", "Number", "Map", "String", "Promise"])
