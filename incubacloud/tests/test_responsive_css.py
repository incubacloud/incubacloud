"""The mobile stylesheet must keep styling classes that actually exist.

The SPA's responsive layer silently died once already: a redesign
renamed the layout classes (``ic-hero``, ``ic-overview-cards`` → the
``rl-*`` family) and nobody updated the ``max-width: 768px`` block, so
it went on styling classes no template rendered any more. The result
looked fine on a laptop and was unusable on a phone — no navigation at
all, a 107px horizontal overflow on every page and page titles painted
over their own buttons.

Nothing in a Python suite can catch a bad layout, but it can catch the
drift that caused it: every class the mobile block styles must still be
rendered by some template, and the classes that make the phone usable
must still be styled.
"""
import re
from pathlib import Path

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase

# Classes the mobile layout depends on. If a redesign renames these,
# this test fails instead of the phone silently breaking again.
_REQUIRED_MOBILE_CLASSES = (
    'ic-sidebar',          # off-canvas drawer
    'ic-sidebar-burger',   # the only way to open it
    'ic-sidebar-backdrop',
    'rl-phead',            # page header row (title + actions)
    'rl-pactions',
)

# Utility/state classes that live in CSS and JS but never appear as a
# literal class attribute in a template.
_NOT_IN_TEMPLATES = {
    'ic-sidebar-open',     # toggled from app.js
    'ic-burger-bar',       # decorative children of the burger
}


def _mobile_block(scss):
    """Return the body of the ``max-width: 768px`` media query."""
    start = scss.index('@media (max-width: 768px)')
    depth, i = 0, scss.index('{', start)
    for pos in range(i, len(scss)):
        if scss[pos] == '{':
            depth += 1
        elif scss[pos] == '}':
            depth -= 1
            if depth == 0:
                return scss[i:pos]
    raise AssertionError('unbalanced media query in app.scss')


class TestResponsiveCssMatchesTemplates(BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        core = Path(get_module_path('incubacloud'))
        cls.scss = (core / 'static/src/app/app.scss').read_text()
        cls.markup = '\n'.join(
            p.read_text()
            for p in (core / 'static/src').rglob('*.xml')
        )
        # The saas layers render into the same shell: the manager adds
        # views to the panel, and the tenant module adds its own (the
        # user list among them) inside each tenant's console.
        for addon in ('incubacloud_saas_manager', 'incubacloud_tenant'):
            path = get_module_path(addon)
            if not path:
                continue
            static = Path(path) / 'static/src'
            if not static.is_dir():
                continue
            cls.markup += '\n'.join(
                p.read_text() for p in static.rglob('*.xml')
            )

    def test_every_styled_class_is_rendered_somewhere(self):
        """No rule may target a class no template renders (dead CSS)."""
        styled = set(re.findall(r'\.((?:ic|rl)-[a-z0-9-]+)', _mobile_block(self.scss)))
        orphans = sorted(
            c for c in styled - _NOT_IN_TEMPLATES if c not in self.markup
        )
        self.assertFalse(
            orphans,
            'mobile CSS styles classes no template renders — the '
            'responsive layer is drifting away from the markup: '
            f'{orphans}',
        )

    def test_the_classes_mobile_depends_on_are_still_styled(self):
        """The drawer, its toggle and the wrapping header must be covered."""
        block = _mobile_block(self.scss)
        missing = [c for c in _REQUIRED_MOBILE_CLASSES if f'.{c}' not in block]
        self.assertFalse(
            missing, f'mobile layout lost its rules for: {missing}',
        )

    def test_the_toggle_is_hidden_outside_the_mobile_block(self):
        """Desktop must not grow a burger: it is opt-in per breakpoint."""
        before = self.scss[:self.scss.index('@media (max-width: 768px)')]
        self.assertIn('.ic-sidebar-burger', before)
        self.assertIn('display: none', before[before.index('.ic-sidebar-burger'):])
