"""The webhook allowlist: what gets published, where, and against what.

The endpoint cannot make a forged signature cheap — verifying one means
hashing the whole body — so the only way to stop paying for it is to
refuse the request before reading it. These cover the document that does
that, the routes it is built from, and the refresh that keeps the source
ranges from going stale, which is the failure mode nothing else sees.
"""
from unittest.mock import patch

import yaml

from odoo.tests.common import TransactionCase

from ..github import edge
from ..github.meta import GitHubMetaError
from ..models import github_webhook_edge as edge_model

RANGES = ["192.30.252.0/22", "185.199.108.0/22"]
EDGE_PROXY = ["198.51.100.0/24"]


class TestWebhookEdgeDocument(TransactionCase):
    """Rendering, in isolation from any host record."""

    def _routes(self):
        return [{
            "hostname": "acme.example.com",
            "service": "acme-19-0-prod-main@docker",
        }]

    def test_the_router_matches_only_the_webhook_path(self):
        rendered = yaml.safe_load(
            edge.build_webhook_edge_yaml(self._routes(), RANGES),
        )
        router = rendered["http"]["routers"]["github-webhook-acme-example-com"]
        self.assertEqual(
            router["rule"],
            "Host(`acme.example.com`) && Path(`/cloud/github/webhook`)",
        )
        self.assertEqual(router["service"], "acme-19-0-prod-main@docker")
        self.assertEqual(router["priority"], edge.ROUTER_PRIORITY)

    def test_a_url_backend_gets_a_service_of_its_own(self):
        rendered = yaml.safe_load(edge.build_webhook_edge_yaml(
            [{"hostname": "www.example.com", "service_url": "http://odoo:8069"}],
            RANGES,
        ))
        name = edge.route_key("www.example.com")
        self.assertEqual(rendered["http"]["routers"][name]["service"], name)
        self.assertEqual(
            rendered["http"]["services"][name],
            {"loadBalancer": {"servers": [{"url": "http://odoo:8069"}]}},
        )

    def test_a_wildcard_router_asks_for_no_certificate_of_its_own(self):
        rendered = yaml.safe_load(edge.build_webhook_edge_yaml(
            [{
                "hostname": "www.example.com",
                "service_url": "http://odoo:8069",
                "tls_domain": "*.example.com",
            }],
            RANGES,
        ))
        tls = rendered["http"]["routers"][
            edge.route_key("www.example.com")
        ]["tls"]
        self.assertEqual(tls["domains"], [{"main": "*.example.com"}])

    def test_a_proxied_route_serves_the_certificate_already_held(self):
        """Measured on Traefik v2.11: an empty TLS section turns TLS on
        and falls back to the default certificate store. A resolver
        named here would make Traefik ignore that store and chase a
        challenge the CDN answers instead of the host."""
        rendered = yaml.safe_load(edge.build_webhook_edge_yaml(
            [{
                "hostname": "acme.example.com",
                "service": "acme-19-0-prod-main@docker",
                "tls_mode": "default",
            }],
            RANGES,
        ))
        router = rendered["http"]["routers"]["github-webhook-acme-example-com"]
        self.assertEqual(router["tls"], {})

    def test_a_proxied_route_ignores_a_wildcard_to_ask_for(self):
        """Neither field names a certificate to obtain any more."""
        rendered = yaml.safe_load(edge.build_webhook_edge_yaml(
            [{
                "hostname": "www.example.com",
                "service_url": "http://odoo:8069",
                "tls_domain": "*.example.com",
                "tls_mode": "default",
            }],
            RANGES,
        ))
        tls = rendered["http"]["routers"][
            edge.route_key("www.example.com")
        ]["tls"]
        self.assertEqual(tls, {})

    def test_a_route_saying_nothing_still_asks_a_ca(self):
        """Absence must not be read as "serve whatever is on disk"."""
        rendered = yaml.safe_load(
            edge.build_webhook_edge_yaml(self._routes(), RANGES),
        )
        router = rendered["http"]["routers"]["github-webhook-acme-example-com"]
        self.assertEqual(router["tls"], {"certResolver": "letsencrypt"})

    def test_behind_a_trusted_proxy_the_allowlist_reads_the_chain(self):
        # Measured on Traefik v2.11: this is the only shape that accepts
        # a delivery arriving through a CDN.
        allow = yaml.safe_load(edge.build_webhook_edge_yaml(
            self._routes(), RANGES, trusted_proxy=True,
        ))["http"]["middlewares"]["github-hooks-only"]["ipWhiteList"]
        self.assertEqual(allow["ipStrategy"], {"depth": 1})
        self.assertEqual(allow["sourceRange"], RANGES)

    def test_without_one_it_reads_the_connection(self):
        # Measured: reading an absent header matches nothing, so a host
        # answering its visitors directly must compare the socket.
        allow = yaml.safe_load(edge.build_webhook_edge_yaml(
            self._routes(), RANGES,
        ))["http"]["middlewares"]["github-hooks-only"]["ipWhiteList"]
        self.assertNotIn("ipStrategy", allow)

    def test_the_body_is_bounded_at_the_edge(self):
        body = yaml.safe_load(edge.build_webhook_edge_yaml(
            self._routes(), RANGES,
        ))["http"]["middlewares"]["github-webhook-body"]
        self.assertEqual(
            body["buffering"]["maxRequestBodyBytes"], edge.MAX_BODY_BYTES,
        )

    def test_an_empty_allowlist_is_refused_outright(self):
        # Publishing one would reject every delivery, silently.
        with self.assertRaises(ValueError):
            edge.build_webhook_edge_yaml(self._routes(), [])

    def test_nothing_to_protect_renders_nothing(self):
        self.assertEqual(edge.build_webhook_edge_yaml([], RANGES), "")

    def test_a_route_missing_its_backend_is_skipped(self):
        self.assertEqual(
            edge.build_webhook_edge_yaml([{"hostname": "x.example.com"}], RANGES),
            "",
        )

    def test_the_same_input_renders_the_same_document(self):
        # The refresh decides whether to ship by comparing documents.
        first = edge.build_webhook_edge_yaml(self._routes(), RANGES)
        second = edge.build_webhook_edge_yaml(self._routes(), RANGES)
        self.assertEqual(first, second)


class TestWebhookEdgeRoutes(TransactionCase):
    """Which hostnames a host publishes, and when it publishes none."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.github_webhook_allowlist = True
        self.env["ir.config_parameter"].sudo().set_param(
            edge_model.RANGES_PARAM, "\n".join(RANGES),
        )
        self.host = self.env["cloud.host"].create({
            "name": "edge-host",
            "ip_address": "192.0.2.60",
            "user": "ubuntu",
            "wildcard_domain": "edge.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "Edge Proj"})
        self.instance = self.env["cloud.instance"].create({
            "name": "acme",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        self.instance._transition("deployed")

    def test_the_feature_is_off_until_it_is_turned_on(self):
        self.settings.github_webhook_allowlist = False
        self.assertEqual(self.host._github_webhook_routes(), [])
        self.assertEqual(self.host._github_webhook_document(), "")

    def test_an_undeployed_instance_publishes_nothing(self):
        self.instance._transition("deleting")
        self.instance._transition("draft")
        self.assertEqual(self.host._github_webhook_routes(), [])

    def test_every_route_carries_how_to_get_its_certificate(self):
        """Decided per host so the panel's own route answers the same
        way as the tenants beside it — it is served by the same proxy
        and reached the same way."""
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        routes = self.host._github_webhook_routes()
        self.assertTrue(routes)
        self.assertTrue(all("tls_mode" in route for route in routes))
        self.assertEqual({r["tls_mode"] for r in routes}, {"acme"})

    def test_behind_the_cdn_the_routes_stop_asking_a_ca(self):
        from ._certs import make_pair
        cert, key = make_pair(["example.com", "*.example.com"])
        self.host.write({
            "behind_cdn": True,
            "tls_default_cert": cert,
            "tls_default_key": key,
        })
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        modes = {
            route["tls_mode"]
            for route in self.host._github_webhook_routes()
            if route["hostname"] == "acme.example.com"
        }
        self.assertEqual(modes, {"default"})

    def test_the_service_follows_the_compose_project_name(self):
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        routes = self.host._github_webhook_routes()
        # The computed hostname counts too: the instance answers there
        # whether or not anybody added an explicit domain.
        self.assertIn("acme.example.com", [r["hostname"] for r in routes])
        self.assertIn(
            self.instance.domain, [r["hostname"] for r in routes],
        )
        self.assertTrue(
            routes[0]["service"].endswith("-prod-main@docker"), routes[0],
        )
        self.assertIn(
            self.instance.doodba_project_name, routes[0]["service"],
        )

    def test_a_redirect_only_domain_is_not_published(self):
        # It never reaches the application, so a router pointed at it
        # would answer nothing.
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "old.example.com",
            "redirect_to": "acme.example.com",
        })
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        hostnames = [
            route["hostname"] for route in self.host._github_webhook_routes()
        ]
        self.assertNotIn("old.example.com", hostnames)
        self.assertIn("acme.example.com", hostnames)

    def test_an_instance_with_no_version_is_skipped(self):
        # The service name cannot be built, and guessing it would point
        # the router at nothing.
        self.instance.odoo_version = False
        self.assertEqual(self.instance._traefik_service_name(), "")
        self.assertEqual(self.host._github_webhook_routes(), [])

    def test_nothing_publishes_until_the_host_runs_the_intended_posture(self):
        """A queued proxy change must not be published against early.

        Measured on Traefik v2.11: a forwarded-chain allowlist on a host
        that still strips the header matches nothing and refuses every
        delivery, and an address-based one on a host already behind a
        CDN refuses them too. Either way the failure is silent, so the
        allowlist waits for the host to actually be running what the
        panel intends.
        """
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        # Nothing intended, nothing shipped: the ordinary direct host.
        self.assertTrue(self.host._github_webhook_document())
        # A proxy is now intended but has not reached the host yet.
        self.host.trusted_proxy_ranges = "\n".join(EDGE_PROXY)
        self.assertEqual(self.host._github_webhook_document(), "")
        # Once shipped, it publishes -- and against the forwarded chain.
        self.host.trusted_proxies_shipped = "\n".join(EDGE_PROXY)
        rendered = yaml.safe_load(self.host._github_webhook_document())
        self.assertEqual(
            rendered["http"]["middlewares"]["github-hooks-only"]
            ["ipWhiteList"]["ipStrategy"],
            {"depth": 1},
        )

    def test_a_proxy_removed_but_still_running_also_holds_it_back(self):
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        self.host.trusted_proxies_shipped = "\n".join(EDGE_PROXY)
        self.assertEqual(self.host._github_webhook_document(), "")

    def test_the_document_needs_both_routes_and_ranges(self):
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        self.assertTrue(self.host._github_webhook_document())
        self.env["ir.config_parameter"].sudo().set_param(
            edge_model.RANGES_PARAM, "",
        )
        # No ranges means publish nothing, never an empty allowlist.
        self.assertEqual(self.host._github_webhook_document(), "")

    def test_a_declared_proxy_changes_the_rendered_strategy(self):
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        direct = yaml.safe_load(self.host._github_webhook_document())
        self.assertNotIn(
            "ipStrategy",
            direct["http"]["middlewares"]["github-hooks-only"]["ipWhiteList"],
        )
        self.host.write({
            "trusted_proxy_ranges": "\n".join(EDGE_PROXY),
            "trusted_proxies_shipped": "\n".join(EDGE_PROXY),
        })
        behind = yaml.safe_load(self.host._github_webhook_document())
        self.assertEqual(
            behind["http"]["middlewares"]["github-hooks-only"]
            ["ipWhiteList"]["ipStrategy"],
            {"depth": 1},
        )

    def test_a_host_is_only_republished_when_its_document_moves(self):
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "acme.example.com",
        })
        self.assertTrue(self.host._github_webhook_needs_push())
        self.host.github_webhook_edge_hash = self.host._github_webhook_digest(
            self.host._github_webhook_document(),
        )
        self.assertFalse(self.host._github_webhook_needs_push())
        # A new tenant on the host is a document change.
        self.env["cloud.instance.domain"].create({
            "instance_id": self.instance.id,
            "hostname": "second.example.com",
        })
        self.assertTrue(self.host._github_webhook_needs_push())


class TestWebhookRangeRefresh(TransactionCase):
    """Keeping the source ranges current, and failing safe when we cannot."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.github_webhook_allowlist = True
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.ICP.set_param(edge_model.RANGES_PARAM, "")
        self.Host = self.env["cloud.host"]

    def _stored(self):
        return self.ICP.get_param(edge_model.RANGES_PARAM, "")

    def test_the_first_read_stores_what_it_found(self):
        with patch.object(edge_model, "fetch_hook_ranges", return_value=RANGES):
            self.assertEqual(self.Host._ensure_github_hook_ranges(), RANGES)
        self.assertEqual(self._stored(), "\n".join(RANGES))

    def test_a_stored_list_is_not_re_read(self):
        self.ICP.set_param(edge_model.RANGES_PARAM, "\n".join(RANGES))
        with patch.object(edge_model, "fetch_hook_ranges") as fetch:
            self.assertEqual(self.Host._ensure_github_hook_ranges(), RANGES)
        fetch.assert_not_called()

    def test_an_unreadable_first_read_publishes_nothing(self):
        with patch.object(
            edge_model, "fetch_hook_ranges",
            side_effect=GitHubMetaError("down"),
        ):
            self.assertEqual(self.Host._ensure_github_hook_ranges(), [])

    def test_a_failed_refresh_keeps_the_previous_list_and_alerts(self):
        # Narrowing an allowlist on bad data is how deliveries stop
        # without a trace.
        self.ICP.set_param(edge_model.RANGES_PARAM, "\n".join(RANGES))
        with patch.object(
            edge_model, "fetch_hook_ranges",
            side_effect=GitHubMetaError("down"),
        ):
            self.assertEqual(self.Host._cron_refresh_github_hook_ranges(), 0)
        self.assertEqual(self._stored(), "\n".join(RANGES))
        Alert = self.env["cloud.alert"].sudo()
        self.assertTrue(Alert.search(
            Alert._dedup_domain(edge_model.WEBHOOK_EDGE_FETCH_ALERT), limit=1,
        ))

    def test_a_changed_list_is_stored_and_alerted(self):
        self.ICP.set_param(edge_model.RANGES_PARAM, "\n".join(RANGES))
        moved = RANGES + ["140.82.112.0/20"]
        with patch.object(edge_model, "fetch_hook_ranges", return_value=moved):
            self.Host._cron_refresh_github_hook_ranges()
        self.assertEqual(self._stored(), "\n".join(moved))
        Alert = self.env["cloud.alert"].sudo()
        self.assertTrue(Alert.search(
            Alert._dedup_domain(edge_model.WEBHOOK_EDGE_CHANGED_ALERT), limit=1,
        ))

    def test_an_unchanged_list_raises_nothing(self):
        self.ICP.set_param(edge_model.RANGES_PARAM, "\n".join(RANGES))
        with patch.object(edge_model, "fetch_hook_ranges", return_value=RANGES):
            self.Host._cron_refresh_github_hook_ranges()
        Alert = self.env["cloud.alert"].sudo()
        self.assertFalse(Alert.search(
            Alert._dedup_domain(edge_model.WEBHOOK_EDGE_CHANGED_ALERT), limit=1,
        ))

    def test_the_refresh_does_nothing_while_the_feature_is_off(self):
        self.settings.github_webhook_allowlist = False
        with patch.object(edge_model, "fetch_hook_ranges") as fetch:
            self.assertEqual(self.Host._cron_refresh_github_hook_ranges(), 0)
        fetch.assert_not_called()
