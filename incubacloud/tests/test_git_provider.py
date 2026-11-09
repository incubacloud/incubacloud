"""Git providers (GitLab) and their credentials.

A provider is one server, its base URL stored canonical; a credential is
a token scoped to a namespace, chosen by the longest namespace that
covers the repository and meets the scopes the operation needs. Where
the panel may connect is ``_network_policy``: ``'any'`` in the core,
``'public'`` in a tenant's panel, which pins the address it checked.
"""
import socket
from datetime import date, timedelta
from unittest.mock import DEFAULT, MagicMock, patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.gitlab import client as client_mod
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
        # A tenant's panel (the saas CI's leg that runs this suite with
        # incubacloud_tenant installed) admits no Git server until the
        # master has named its own servers: one is named, which is no
        # test's. The core does not read the parameter.
        cls.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.platform_addresses", "192.0.2.1",
        )
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


class TestBaseUrlChangeDropsTheTokens(_ProviderCase):
    """A token is for one server: a new base URL asks for it again."""

    def test_a_new_server_or_prefix_empties_every_token(self):
        for base in ("https://git.acme.tld", "https://gitlab.com/prefix"):
            with self.subTest(base=base):
                cred = self._cred("acme", scopes="api", token_user="bot")
                self.provider.write({"base_url": base})
                self.assertFalse(cred.sudo().token)
                self.assertFalse(cred.scopes)
                self.assertFalse(cred.token_user)
                cred.unlink()

    def test_the_same_base_url_keeps_them(self):
        cred = self._cred("acme", scopes="api", token_user="bot")
        self.provider.write({"base_url": "https://GITLAB.com/", "name": "Renamed"})
        self.assertEqual(cred.sudo().token, "glpat-abcdefgh12")
        self.assertEqual(cred.scopes, "api")
        self.assertEqual(cred.token_user, "bot")

    def test_a_credential_without_token_reads_as_public(self):
        self._cred("acme")
        self.provider.write({"base_url": "https://git.acme.tld"})
        self.assertIsNone(self.provider._git_credential("acme/addons"))


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

    def test_git_prefers_a_read_only_credential_that_covers_it(self):
        # A token that can write travels inside the image of 11.0 to 19.0:
        # one that only reads is taken first (security review, 8-nov-2026).
        self._cred("acme/sub", scopes="api")
        reader = self._cred("acme", scopes="read_repository", token="glpat-readonly12")
        self.assertEqual(
            self.provider._git_credential("acme/sub/x"), ("oauth2", "glpat-readonly12"),
        )
        reader.unlink()
        deploy = self._cred(
            "acme", kind="deploy", username="gitlab+deploy-token-7",
            token="gldt-deploy1234",
        )
        self.assertEqual(
            self.provider._git_credential("acme/sub/x"),
            ("gitlab+deploy-token-7", "gldt-deploy1234"),
        )
        deploy.unlink()
        # Only one that writes: that one, as before.
        self.assertEqual(
            self.provider._git_credential("acme/sub/x"), ("oauth2", "glpat-abcdefgh12"),
        )

    def test_a_credential_says_whether_it_can_write(self):
        self.assertTrue(self._cred("a", scopes="api")._writes())
        self.assertTrue(self._cred("b", scopes="read_api write_repository")._writes())
        self.assertFalse(self._cred("c", scopes="read_repository read_api")._writes())
        self.assertFalse(self._cred(
            "d", kind="deploy", username="gitlab+deploy-token-7",
        )._writes())

    def test_a_token_whose_scopes_were_not_read_counts_as_one_that_writes(self):
        # Empty scopes on a token that is not a deploy token say nothing
        # of it: unread, a save past the cap, or a 401.
        self.assertTrue(self._cred("a")._writes())
        self.assertFalse(self._cred(
            "d", kind="deploy", username="gitlab+deploy-token-7",
        )._writes())

    def test_git_does_not_prefer_a_token_whose_scopes_were_not_read(self):
        self._cred("acme", token="glpat-unchecked12")
        self._cred("", scopes="read_repository", token="glpat-readonly12")
        self.assertEqual(
            self.provider._git_credential("acme/x"), ("oauth2", "glpat-readonly12"),
        )

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
            type(self.Provider), "_network_policy", autospec=True,
            return_value="public",
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
        Alert = type(self.env["cloud.alert"])
        # The first alert fails; the second is raised for real.
        with patch.object(
            Alert, "raise_alert", autospec=True, wraps=Alert.raise_alert,
            side_effect=[RuntimeError("boom"), DEFAULT],
        ) as raise_alert:
            self.Provider._cron_check_git_providers()
        self.assertEqual(raise_alert.call_count, 2)
        self.assertEqual(len(self._alerts()), 1)

    def test_warning_days(self):
        self.assertEqual(provider_mod.EXPIRY_WARNING_DAYS, 14)


class TestCredentialIntrospection(_ProviderCase):
    """What GitLab says of a token is read, not typed."""

    def _introspect(self, cred, token_self, user=None):
        """Introspect *cred* with the API answering *token_self* and *user*.

        :rtype: MagicMock (the client class)
        """
        client = MagicMock(spec=client_mod.GitLabClient)
        if isinstance(token_self, Exception):
            client.token_self.side_effect = token_self
        else:
            client.token_self.return_value = token_self
        client.current_user.return_value = user or {"username": "qa-bot"}
        with patch.object(
            provider_mod, "GitLabClient", autospec=True, return_value=client,
        ) as made:
            cred._introspect()
        return made, client

    def test_a_read_repository_token_has_its_scopes_and_expiry_but_no_user(self):
        cred = self._cred("acme")
        made, client = self._introspect(cred, {
            "scopes": ["read_repository"], "expires_at": "2027-01-31",
            "active": True,
        })
        self.assertEqual(cred.scopes, "read_repository")
        self.assertEqual(cred.expires_on, date(2027, 1, 31))
        self.assertFalse(cred.token_user)
        client.current_user.assert_not_called()
        self.assertEqual(made.call_args.args[:2], (
            "https://gitlab.com/api/v4", "glpat-abcdefgh12",
        ))

    def test_an_api_token_names_its_user(self):
        for scope in ("api", "read_api"):
            with self.subTest(scope=scope):
                cred = self._cred(f"acme-{scope}")
                self._introspect(cred, {"scopes": [scope, "read_repository"]})
                self.assertEqual(cred.token_user, "qa-bot")

    def test_a_401_leaves_the_credential_without_api(self):
        cred = self._cred(
            "acme", kind="deploy", username="gitlab+deploy-token-1",
            scopes="api",
        )
        self._introspect(cred, client_mod.GitLabError("auth", status=401))
        self.assertEqual(cred.scopes, "")
        self.assertFalse(cred.token_user)

    def test_another_failure_is_raised_without_the_token(self):
        cred = self._cred("acme")
        with self.assertRaises(UserError) as caught:
            self._introspect(cred, client_mod.GitLabError("unreachable"))
        self.assertNotIn("glpat-abcdefgh12", str(caught.exception))


class TestCredentialStaysWithItsTokenAndServer(_ProviderCase):
    """What was read of a token is dropped with it, and a token never
    moves to another server."""

    def test_a_new_token_drops_what_was_read_of_the_old_one(self):
        cred = self._cred("acme", scopes="read_repository", token_user="bot")
        cred.write({"token": "glpat-newtoken1234"})
        self.assertFalse(cred.scopes)
        self.assertFalse(cred.token_user)
        self.assertTrue(cred._writes())
        # A write without a token keeps them.
        cred.sudo().write({"scopes": "read_repository", "token_user": "bot"})
        cred.write({"expires_on": date(2027, 1, 31)})
        self.assertEqual(cred.scopes, "read_repository")
        self.assertEqual(cred.token_user, "bot")

    def test_a_credential_never_moves_to_another_provider(self):
        other = self.Provider.create({
            "name": "Other", "base_url": "https://git.other.tld",
        })
        cred = self._cred("acme")
        with self.assertRaisesRegex(UserError, "another Git provider"), \
                self.cr.savepoint():
            cred.write({"provider_id": other.id})
        with self.assertRaisesRegex(UserError, "another Git provider"), \
                self.cr.savepoint():
            other.write({"credential_ids": [Command.link(cred.id)]})
        self.assertEqual(cred.provider_id, self.provider)
        # Its own provider again changes nothing.
        cred.write({"provider_id": self.provider.id})
        self.assertEqual(cred.provider_id, self.provider)

    def test_a_copy_takes_no_token(self):
        cred = self._cred("acme")
        self.assertFalse(cred.copy({"namespace": "acme-copy"}).sudo().token)
