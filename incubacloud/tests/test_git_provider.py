"""Git providers (GitLab) and their credentials (plan-gitlab.md §2.1-2.2, §3).

A provider is one server, its base URL stored canonical; a credential is
a token scoped to a namespace, chosen by the longest namespace that
covers the repository and meets the scopes the operation needs. Where
the panel may connect is ``_network_policy``: ``'any'`` in the core,
``'public'`` in a tenant's panel, which pins the address it checked.
"""
import socket
from datetime import date, timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.models import cloud_git_provider as provider_mod
from odoo.addons.incubacloud.tests import _certs

_OUTBOUND = "odoo.addons.incubacloud.net.outbound.getaddrinfo"


def _infos(*addresses):
    """Return what ``getaddrinfo`` answers for *addresses*.

    :rtype: list
    """
    infos = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        infos.append((family, socket.SOCK_STREAM, 6, "", (address, 443)))
    return infos


class _ProviderCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Provider = cls.env["cloud.git.provider"]
        cls.Credential = cls.env["cloud.git.credential"]
        cls.provider = cls.Provider.create({
            "name": "GitLab.com", "base_url": "https://GitLab.com/",
        })

    def _cred(self, namespace, kind="project", scopes="", **vals):
        """Create a credential of the test provider.

        :rtype: cloud.git.credential
        """
        cred = self.Credential.create({
            "provider_id": self.provider.id, "namespace": namespace,
            "kind": kind, "token": "glpat-abcdefgh12",
        } | vals)
        cred.sudo().write({"scopes": scopes})
        return cred


class TestProviderBaseUrl(_ProviderCase):

    def test_stored_canonical(self):
        self.assertEqual(self.provider.base_url, "https://gitlab.com")
        self.assertEqual(self.provider.authority, "gitlab.com")
        self.assertEqual(self.provider.api_url, "https://gitlab.com/api/v4")

    def test_refused_base_urls(self):
        for base in (
            "http://git.acme.tld",
            "https://user@git.acme.tld",
            "https://git.acme.tld/?x=1",
            "https://github.com",
            "https://api.github.com",
            "https://gít.acme.tld",
            "https://git.acme.tld/a/../b",
        ):
            with self.subTest(base=base), self.assertRaises(ValidationError):
                self.Provider.create({"name": "x", "base_url": base})

    def test_one_provider_per_server(self):
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self.Provider.create({"name": "dup", "base_url": "https://gitlab.com"})

    def test_audit_has_no_values(self):
        self.env["cloud.audit.log"].search([]).unlink()
        cred = self._cred("acme", token="glpat-supersecret99")
        cred.write({"token": "glpat-othersecret99"})
        details = " ".join(
            self.env["cloud.audit.log"].search([]).mapped("details"),
        )
        self.assertIn("namespace 'acme'", details)
        self.assertIn("token", details)
        self.assertNotIn("supersecret", details)
        self.assertNotIn("othersecret", details)


class TestCaBundle(_ProviderCase):

    def test_ca_bundle_accepted_with_fingerprint(self):
        pem = _certs.ca_pem()
        self.provider.write({"ca_bundle": pem})
        fingerprints = self.provider.ca_fingerprints.splitlines()
        self.assertEqual(len(fingerprints), 1)
        self.assertRegex(fingerprints[0], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")

    def test_private_key_refused(self):
        bundle = _certs.ca_pem() + _certs.make_pair()[1]
        with self.assertRaises(ValidationError):
            self.provider.write({"ca_bundle": bundle})

    def test_leaf_only_refused(self):
        with self.assertRaises(ValidationError):
            self.provider.write({"ca_bundle": _certs.leaf_pem()})

    def test_too_large_refused(self):
        with self.assertRaises(ValidationError):
            self.provider.write({"ca_bundle": _certs.ca_pem() * 150})

    def test_garbage_refused(self):
        with self.assertRaises(ValidationError):
            self.provider.write({
                "ca_bundle": "-----BEGIN CERTIFICATE-----\nnope\n"
                "-----END CERTIFICATE-----\n",
            })


class TestCredentialFor(_ProviderCase):

    def test_longest_namespace_by_segments(self):
        server = self._cred("", scopes="api")
        group = self._cred("acme", kind="deploy", username="gitlab+deploy-token-7")
        sub = self._cred("acme/sub")
        other = self._cred("acme-other")
        self.assertEqual(self.provider._credential_for("acme/sub/addons", "git_read"), sub)
        self.assertEqual(self.provider._credential_for("acme/addons", "git_read"), group)
        self.assertEqual(self.provider._credential_for("acme-other/x", "git_read"), other)
        self.assertEqual(self.provider._credential_for("zeta/x", "git_read"), server)

    def test_need_filters_before_length(self):
        server = self._cred("", scopes="api")
        self._cred("acme", scopes="read_repository")
        read_api = self._cred("acme/sub", scopes="read_api read_repository")
        self.assertEqual(self.provider._credential_for("acme/sub/x", "api_read"), read_api)
        self.assertEqual(self.provider._credential_for("acme/sub/x", "api_write"), server)

    def test_deploy_token_never_meets_the_api(self):
        self._cred(
            "acme", kind="deploy", username="gitlab+deploy-token-7", scopes="api",
        )
        with self.assertRaisesRegex(UserError, "scope"):
            self.provider._credential_for("acme/x", "api_read")

    def test_nothing_covers(self):
        self._cred("acme")
        with self.assertRaisesRegex(UserError, "No credential"):
            self.provider._credential_for("zeta/x", "git_read")

    def test_default_usernames(self):
        self.assertEqual(self._cred("a").username, "oauth2")
        with self.assertRaises(ValidationError):
            self._cred("b", kind="deploy")

    def test_namespace_normalized_and_unique(self):
        cred = self._cred(" /Acme/Sub/ ")
        self.assertEqual(cred.namespace, "acme/sub")
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self._cred("acme/sub")
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self._cred("")
            self._cred("")

    def test_token_shape(self):
        for token in ("short", "tok@evil", "tok:en12345", "tok/en12345"):
            with self.subTest(token=token), self.assertRaises(ValidationError):
                self._cred("t", token=token)


@tagged("post_install", "-at_install")
class TestCredentialAccess(_ProviderCase):

    def test_consultant_cannot_read_the_token(self):
        self._cred("acme")
        consultant = new_test_user(
            self.env, login="git-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        creds = self.Credential.with_user(consultant).search([])
        self.assertTrue(creds)
        creds.read(["namespace", "scopes"])
        with self.assertRaises(AccessError):
            creds.read(["token"])
        with self.assertRaises(AccessError):
            creds.write({"namespace": "x"})

    def test_rotation_reaches_the_token(self):
        self._cred("acme")
        stats = self.env["cloud.settings"].sudo()._rotate_all_secrets()
        self.assertIn("cloud_git_credential.token", stats)


class TestNetworkPolicy(_ProviderCase):

    def test_core_policy_is_any_and_pins_nothing(self):
        self.assertEqual(self.Provider._network_policy(), "any")
        with patch(_OUTBOUND, return_value=_infos("10.0.0.5")) as dns:
            self.assertIsNone(self.provider._resolve_pin())
            self.Provider.create({"name": "lan", "base_url": "https://10.0.0.1"})
        dns.assert_not_called()

    def _public(self):
        """Patch the provider model's policy to ``'public'``."""
        return patch.object(
            type(self.Provider), "_network_policy", lambda self: "public",
        )

    def test_public_refuses_non_public_addresses(self):
        for addresses in (
            ("10.0.0.5",), ("127.0.0.1",), ("169.254.169.254",),
            ("169.254.1.1",), ("fd00::1",), ("::ffff:10.0.0.1",),
            ("192.168.1.1",), ("140.82.112.3", "10.0.0.5"),
        ):
            with self.subTest(addresses=addresses), self._public(), patch(
                _OUTBOUND, return_value=_infos(*addresses),
            ), self.assertRaisesRegex(UserError, "public"), self.cr.savepoint():
                self.Provider.create({
                    "name": "x", "base_url": "https://git.example.org",
                })

    def test_public_pins_the_checked_address(self):
        with self._public(), patch(
            _OUTBOUND, return_value=_infos("2606:4700::1", "172.65.251.78"),
        ):
            self.assertEqual(self.provider._resolve_pin(), "172.65.251.78")


class TestExpiryCron(_ProviderCase):

    def _alerts(self):
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "git_token_expiring"), ("state", "=", "active"),
        ])

    def test_warns_and_clears_after_rotation(self):
        soon = self._cred("acme", expires_on=date.today() + timedelta(days=3))
        self._cred("later", expires_on=date.today() + timedelta(days=90))
        self.Provider._cron_check_git_providers()
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("acme", alerts.message)
        soon.write({"expires_on": date.today() + timedelta(days=365)})
        self.Provider._cron_check_git_providers()
        self.assertFalse(self._alerts())

    def test_one_broken_provider_does_not_stop_the_others(self):
        self._cred("acme", expires_on=date.today())
        other = self.Provider.create({
            "name": "Other", "base_url": "https://git.example.org",
        })
        self.Credential.create({
            "provider_id": other.id, "namespace": "z",
            "token": "glpat-abcdefgh12", "expires_on": date.today(),
        })
        real = type(self.env["cloud.alert"]).raise_alert
        calls = []

        def flaky(alert, code, message, **kw):
            calls.append(kw.get("key"))
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(alert, code, message, **kw)

        with patch.object(type(self.env["cloud.alert"]), "raise_alert", flaky):
            self.Provider._cron_check_git_providers()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self._alerts()), 1)

    def test_warning_days(self):
        self.assertEqual(provider_mod.EXPIRY_WARNING_DAYS, 14)
