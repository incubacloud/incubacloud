"""Who may put Grafana in a frame, as the gateway fragment declares it.

Tenant panels embed Grafana from their own origins, so Grafana has to
name them in ``frame-ancestors``. On 8-sep-2026 the only list on Cloud 1
was the host's entrypoint default, which named the operator's panel
alone; every tenant's dashboards went blank and nothing reported it,
because a frame blocked by CSP still fires ``load``.

The policy now lives on Grafana's own router in
``docs/observability-traefik-metrics-gateway.yml``, the file installed on
the central's host. These pin what that file has to keep saying.

They need the repository checkout, which CI has. The production image does
not: its build removes every directory of this repository that is not an
addon, ``docs/`` included, so the deploy's boot test skips them there
rather than erroring on a file that was never meant to ship.
"""
import pathlib
import re

import yaml

from odoo.tests.common import BaseCase


_GATEWAY = (
    pathlib.Path(__file__).resolve().parents[2]
    / "docs" / "observability-traefik-metrics-gateway.yml"
)
_ZONE_SOURCE = "https://*.incubacloud.io"


def _gateway():
    """Return the parsed gateway fragment.

    :return: the ``http`` mapping of the fragment.
    :rtype: dict
    """
    return yaml.safe_load(_GATEWAY.read_text())["http"]


def _frame_ancestors(headers):
    """Return the ``frame-ancestors`` sources of a headers middleware.

    :param headers: the ``headers`` mapping of a Traefik middleware.
    :return: the source list, empty when no such directive is set.
    :rtype: list[str]
    """
    match = re.search(
        r"frame-ancestors\s+([^;]+)",
        headers.get("contentSecurityPolicy") or "",
    )
    return match.group(1).split() if match else []


class TestMetricsGatewayFrameguard(BaseCase):
    """The gateway lets panels anywhere in the zone frame Grafana."""

    def setUp(self):
        """Skip where the repository's ``docs/`` was stripped out.

        Only the whole directory being absent skips: a checkout that has
        ``docs/`` but lost the fragment is a rename nobody followed up,
        and reading it fails loudly below.
        """
        super().setUp()
        if not _GATEWAY.parent.is_dir():
            self.skipTest(
                "docs/ is not in this build (the production image keeps "
                "addons only); CI checks the gateway fragment."
            )

    def _grafana_headers(self):
        """Return every headers middleware the Grafana router applies.

        :return: one ``headers`` mapping per middleware that has one.
        :rtype: list[dict]
        """
        http = _gateway()
        names = http["routers"]["metrics-grafana"].get("middlewares") or []
        middlewares = http.get("middlewares") or {}
        for name in names:
            self.assertIn(
                name, middlewares,
                f"metrics-grafana references {name!r}, which the file does "
                "not define: Traefik answers 500 on that router.",
            )
        return [
            middlewares[name]["headers"]
            for name in names
            if "headers" in middlewares[name]
        ]

    def test_grafana_router_lets_every_panel_in_the_zone_frame_it(self):
        """A tenant born tomorrow must not need this file re-shipped."""
        sources = [
            source
            for headers in self._grafana_headers()
            for source in _frame_ancestors(headers)
        ]
        self.assertIn(
            _ZONE_SOURCE, sources,
            "Grafana's router must allow the whole zone to frame it: "
            "tenant panels embed it from their own origins.",
        )

    def test_grafana_router_sends_no_frame_options(self):
        """``X-Frame-Options`` cannot name another origin.

        Paired with the list, it would refuse the very frames the list
        lets through, in every browser that honours it over CSP.
        """
        for headers in self._grafana_headers():
            self.assertFalse(headers.get("frameDeny"))
            self.assertFalse(headers.get("customFrameOptionsValue"))

    def test_only_grafana_is_framable(self):
        """The data plane carries no framing exception of its own."""
        http = _gateway()
        middlewares = http.get("middlewares") or {}
        for name, router in http["routers"].items():
            if name == "metrics-grafana":
                continue
            for middleware in router.get("middlewares") or []:
                headers = middlewares.get(middleware, {}).get("headers", {})
                self.assertNotIn(
                    _ZONE_SOURCE, _frame_ancestors(headers),
                    f"{name} would let the whole zone frame it.",
                )
