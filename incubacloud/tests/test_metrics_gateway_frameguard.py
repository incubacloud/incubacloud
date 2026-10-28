"""Who may put Grafana in a frame, as the gateway fragment declares it.

Tenant panels embed Grafana from their own origins, so Grafana has to
name them in ``frame-ancestors``. On 8-sep-2026 the only list on Cloud 1
was the host's entrypoint default, which named the operator's panel
alone; every tenant's dashboards went blank and nothing reported it,
because a frame blocked by CSP still fires ``load``.

The policy now lives on Grafana's own router in
``incubacloud/data/observability-traefik-metrics-gateway.yml``, the file
installed on the central's host. These pin what that file has to keep
saying. It ships inside the module (until 20-oct-2026 it sat in
``docs/``, which the production image strips, so the deploy's own run of
these skipped them), and they run wherever the module does.
"""
import pathlib
import re

import yaml

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models.observability_central_executor import (
    ObservabilityCentralExecutor,
)


_GATEWAY = (
    pathlib.Path(__file__).resolve().parents[1]
    / "data" / "observability-traefik-metrics-gateway.yml"
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

    def test_every_router_names_its_entrypoints_and_none_is_direct(self):
        """Nothing here may answer on the direct entrypoint (piece 9)."""
        for name, router in _gateway()["routers"].items():
            with self.subTest(router=name):
                self.assertTrue(router.get("entryPoints"))
                self.assertNotIn("direct", router["entryPoints"])

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


class TestOperatorRoutesAreNotPublic(BaseCase):
    """Every operator route vmauth serves is refused from outside.

    The gateway fragment is installed by hand, and vmauth's routes are
    rendered by code. A route added to one and not the other is public
    the moment the central is redeployed — which is exactly what a new
    deletion route would have been.
    """

    def test_every_operator_prefix_is_denied(self):
        # Rendered without a database: the method only reads its
        # arguments and the account route builder.
        executor = object.__new__(ObservabilityCentralExecutor)
        cfg = yaml.safe_load(
            executor._vmauth_config([], "op", "YWRtaW46eA==", "key"),
        )
        operator = next(u for u in cfg["users"] if u["username"] == "operator")
        prefixes = {
            "/" + src.strip("/").split("/", 1)[0] + "/"
            for entry in operator["url_map"]
            for src in entry["src_paths"]
        }
        rule = _gateway()["routers"]["metrics-operator-denied"]["rule"]
        for prefix in prefixes:
            self.assertIn(
                f"PathPrefix(`{prefix}`)", rule,
                f"{prefix} is an operator route that the public gateway "
                "does not deny",
            )

    def test_the_data_router_serves_the_account_prefixes_only(self):
        """vmauth answers /flags, /metrics and /debug/pprof/ to anyone.

        So the public router must not hand it the whole host: measured
        on 2026-09-27, a credential-less POST to those got 200 from
        outside. Pinned to exactly what an account route starts with.
        """
        executor = object.__new__(ObservabilityCentralExecutor)
        account_prefixes = {
            "/" + src.strip("/").split("/", 1)[0] + "/"
            for entry in executor._account_url_map("acct_x")
            for src in entry["src_paths"]
        }
        rule = _gateway()["routers"]["metrics-data"]["rule"]
        self.assertEqual(
            set(re.findall(r"PathPrefix\(`([^`]+)`\)", rule)),
            account_prefixes,
            "the public data router must name exactly the account "
            "prefixes; anything wider reaches vmauth's own endpoints",
        )
