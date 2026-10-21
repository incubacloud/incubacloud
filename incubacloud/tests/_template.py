"""The doodba template a test renders its stacks with.

The code's default pin (``cloud.settings.copier_template_ref``) is the
newest template, and only a fresh database takes it: the CI's does,
devel's and production's keep the version stored in their settings. A
test that renders a stack without saying which template tests whichever
its database holds, and passed in devel while it failed in CI when the
default moved to template v10 (21-oct-2026).
"""

#: The template before v10, which production runs: the egress sidecars
#: (``proxy_general``, ``odoo_net_setup``) and a staging's Traefik labels
#: on ``odoo``. Template v10's gatekeeper is tested in
#: ``test_template_v10_stack``.
OLD_TEMPLATE_REF = "v9.7.0"


def pin_template(env, ref=OLD_TEMPLATE_REF):
    """Render the test's stacks with doodba template *ref*.

    :param env: the test's environment; the change is rolled back with it.
    :param str ref: the template's tag.
    """
    env["cloud.settings"].sudo()._get().copier_template_ref = ref
