"""
Tier 2 — ORM integration tests for cloud.host.
Verifies Traefik template defaults, required fields, and the
``_release_external_resources`` lifecycle hook fired before a host
is archived or unlinked.
"""

import asyncio
import shutil
import subprocess
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
from asyncssh.misc import _ACMWrapper

from odoo import http
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.incubacloud.models.host_hardening_executor import (
    HostHardeningExecutor,
)


class TestCloudHostTraefikDefaults(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    def _create(self, **kw):
        return self.Host.create(
            {
                "name": "Test Host",
                "ip_address": "10.0.0.1",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
            }
            | kw
        )

    def test_create_applies_defaults(self):
        """Host without traefik fields gets templates."""
        host = self._create()
        self.assertTrue(host.traefik_config_yml)
        self.assertTrue(host.traefik_inverseproxy_yaml)
        self.assertTrue(host.traefik_yml)

    def test_create_preserves_explicit(self):
        """Explicit traefik fields are not overwritten."""
        custom = "# custom config"
        host = self._create(traefik_config_yml=custom)
        self.assertEqual(host.traefik_config_yml, custom)

    def test_create_empty_string_gets_defaults(self):
        """Empty strings from frontend get replaced."""
        host = self._create(
            traefik_config_yml="",
            traefik_inverseproxy_yaml="",
            traefik_yml="",
        )
        self.assertTrue(host.traefik_config_yml)
        self.assertTrue(host.traefik_inverseproxy_yaml)
        self.assertTrue(host.traefik_yml)

    def test_defaults_contain_traefik(self):
        """Templates contain recognizable Traefik config."""
        host = self._create()
        self.assertIn("traefik", host.traefik_yml.lower())
        self.assertIn(
            "traefik",
            host.traefik_inverseproxy_yaml.lower(),
        )

    def test_defaults_acme_storage_is_persistent(self):
        """ACME storage must point at the mounted ``acme`` volume, not a
        relative path (which Traefik would write to the container's
        ephemeral FS, losing certs on every recreate)."""
        host = self._create()
        self.assertIn(
            'storage: "/etc/traefik/acme/acme.json"',
            host.traefik_yml,
        )
        self.assertNotIn('storage: "acme.json"', host.traefik_yml)


class TestCloudHostReleaseHook(TransactionCase):
    """The ``_release_external_resources`` hook must fire on every
    transition that disables or removes a host so derived modules
    can release attached external resources without having to
    override write/unlink themselves."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]
        self.host = self.Host.create(
            {
                "name": "Hook Target",
                "ip_address": "10.0.0.2",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
            }
        )

    def test_hook_is_noop_by_default(self):
        """Default implementation must be a no-op so installs without
        any extension keep working unchanged."""
        result = self.host._release_external_resources()
        self.assertIsNone(result)

    def test_write_active_false_calls_hook(self):
        """Archiving an active host triggers the hook before super()."""
        with patch.object(
            type(self.host),
            "_release_external_resources",
            autospec=True,
        ) as hook:
            self.host.write({"active": False})
        hook.assert_called_once()
        called_with = hook.call_args.args[0]
        self.assertEqual(called_with, self.host)

    def test_write_active_true_does_not_call_hook(self):
        """Re-activating a host must not fire the release hook."""
        self.host.write({"active": False})
        with patch.object(
            type(self.host),
            "_release_external_resources",
            autospec=True,
        ) as hook:
            self.host.write({"active": True})
        hook.assert_not_called()

    def test_write_other_field_does_not_call_hook(self):
        """Writing unrelated fields keeps the hook untouched."""
        with patch.object(
            type(self.host),
            "_release_external_resources",
            autospec=True,
        ) as hook:
            self.host.write({"name": "Renamed"})
        hook.assert_not_called()

    def test_write_active_false_skips_already_inactive(self):
        """Already-inactive hosts inside the recordset are excluded
        from the hook call — only the transitioning ones get released."""
        inactive = self.Host.create(
            {
                "name": "Already off",
                "ip_address": "10.0.0.3",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
                "active": False,
            }
        )
        recordset = self.host | inactive
        with patch.object(
            type(self.host),
            "_release_external_resources",
            autospec=True,
        ) as hook:
            recordset.write({"active": False})
        hook.assert_called_once()
        called_with = hook.call_args.args[0]
        self.assertEqual(called_with, self.host)
        self.assertNotIn(inactive, called_with)

    def test_unlink_calls_hook(self):
        """Unlinking a host fires the hook before super().unlink()."""
        with patch.object(
            type(self.host),
            "_release_external_resources",
            autospec=True,
        ) as hook:
            self.host.unlink()
        hook.assert_called_once()


# Note: the ``/cloud/delete_host`` route is a one-line branch on top of
# the model-level ``write/unlink`` covered above — calling it from a
# bare TransactionCase requires faking the full ``odoo.http.request``
# proxy, which is more plumbing than the branch is worth. The
# end-to-end behaviour is checked manually per the plan's Verificación
# table; if that ever changes, an ``HttpCase`` test with authentication
# is the right tool.


class TestCloudHostRBACGates(TransactionCase):
    """Security regression suite: the lifecycle hook reorganisation
    must not weaken any existing manager-role gate on
    ``cloud.host.write``, ``cloud.host.unlink`` or the
    ``/cloud/delete_host`` route. A non-manager who lands a stray
    call on any of those paths must still get an ``AccessError``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Some optional addons add NOT NULL columns to res_partner
        # without server defaults — ``new_test_user`` then trips them
        # at partner-create. Mirror the workaround used by the wider
        # security suite so this file stays self-contained.
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if "char" in dtype or "text" in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner ALTER COLUMN "{col}" SET DEFAULT {default}'
            )

    def setUp(self):
        super().setUp()
        self.consultant = new_test_user(
            self.env,
            login="host_rbac_consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        self.host = self.env["cloud.host"].create(
            {
                "name": "Gate Target",
                "ip_address": "10.0.0.60",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
            }
        )

    def test_non_manager_cannot_archive(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).write({"active": False})
        self.assertTrue(self.host.exists())
        self.assertTrue(self.host.active)

    def test_non_manager_cannot_unlink(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).unlink()
        self.assertTrue(self.host.exists())

    def test_archive_writes_audit_entry(self):
        """``active`` is in ``_AUDIT_TRACKED_FIELDS`` — archive
        transitions must keep generating a Config-changed audit entry
        after the hook reorganisation."""
        before = self.env["cloud.audit.log"].search_count(
            [
                ("host_id", "=", self.host.id),
                ("action", "=", "Config changed"),
            ]
        )
        self.host.write({"active": False})
        after = self.env["cloud.audit.log"].search_count(
            [
                ("host_id", "=", self.host.id),
                ("action", "=", "Config changed"),
            ]
        )
        self.assertEqual(after - before, 1)

    def test_unlink_writes_audit_entry(self):
        """``unlink`` leaves a ``Host deleted`` row in the audit log
        (``host_id`` is ``ondelete='set null'``, so the entry survives
        the host going away)."""
        host_name = self.host.name
        before = self.env["cloud.audit.log"].search_count(
            [
                ("action", "=", "Host deleted"),
                ("details", "=", host_name),
            ]
        )
        self.host.unlink()
        after = self.env["cloud.audit.log"].search_count(
            [
                ("action", "=", "Host deleted"),
                ("details", "=", host_name),
            ]
        )
        self.assertEqual(after - before, 1)


class TestCloudDeleteHostRoute(TransactionCase):
    """``/cloud/delete_host`` no-traefik branch must choose between
    ``unlink`` (truly empty host) and ``archive`` (host with job
    history) so the ``cloud.job.host_id`` FK doesn't trip when a host
    accumulates a job before Traefik is deployed.

    Concrete trigger that motivated the branch: on-demand VPS chain
    (``tenant_vps_provision → host_hardening → full_setup``) — the
    provision job succeeds and lands on ``cloud.job`` but the chain
    breaks before Traefik is deployed, so ``host.traefik_deployed`` is
    still False while ``cloud_job_count >= 1``. The old ``unlink``
    branch then violated the FK and the user got the Odoo "Another
    model is using the record …" pop-up.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Same NOT NULL workaround the wider security suite uses so the
        # manager test user can be created without tripping on optional
        # res_partner columns added by side modules.
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if "char" in dtype or "text" in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner ALTER COLUMN "{col}" SET DEFAULT {default}'
            )

    def setUp(self):
        super().setUp()
        self.manager = new_test_user(
            self.env,
            login="host_delete_manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.host = self.env["cloud.host"].create(
            {
                "name": "Delete Target",
                "ip_address": "10.0.0.99",
                "user": "ubuntu",
                "wildcard_domain": "delete.example.com",
            }
        )
        # Ensure the precondition the no-traefik branch reacts to.
        # ``traefik_deployed`` defaults to False but we make the
        # assumption explicit so a future model change can't silently
        # invalidate the tests.
        self.assertFalse(self.host.traefik_deployed)

    def _invoke_delete(self, host_id):
        """Drive ``cloud_delete_host`` against the test cursor.

        The route reads ``odoo.http.request`` for env + httprequest; we
        patch the module-level proxy with a ``MagicMock`` spec'd
        against the real ``http.Request`` class so any access outside
        the API surface fails loudly (per project policy on mocks).
        """
        from odoo.addons.incubacloud.controllers import data_load
        from odoo.addons.incubacloud.controllers._data_load import (
            _routes_crud,
        )

        fake_request = MagicMock(spec=http.Request)
        # Spec'd against the real class so any access outside the
        # documented API surface fails loudly. The route plus the
        # RBAC helper only touch ``request.env``; if a future change
        # starts reading another attribute, this mock raises
        # ``AttributeError`` and the test points the way.
        fake_request.env = self.env(user=self.manager)
        # CrudMixin's ``self._sec()`` and ``request.env`` lookups
        # resolve via ``odoo.http.request``, the werkzeug LocalProxy.
        # Patch the proxy globally so the resolution through
        # ``data_load.request`` and ``_routes_crud.request`` (each its
        # own module-local import) both land on the fake.
        ctrl = data_load.CloudDataLoadController()
        with (
            patch.object(http, "request", fake_request),
            patch.object(data_load, "request", fake_request),
            patch.object(_routes_crud, "request", fake_request),
        ):
            return ctrl.cloud_delete_host(host_id)

    def test_empty_host_no_traefik_is_unlinked(self):
        """Truly-empty host (no jobs, no Traefik) takes the unlink
        fast path — same behaviour as before the F-branch fix."""
        host_id = self.host.id
        result = self._invoke_delete(host_id)
        self.assertEqual(result, {"ok": True})
        self.assertFalse(
            self.env["cloud.host"].browse(host_id).exists(),
            "Empty host must be unlinked, not archived.",
        )

    def test_host_with_jobs_is_archived_and_preserves_jobs(self):
        """Host carrying any ``cloud.job`` row must be archived (not
        unlinked) so the FK stays valid and the job history survives.

        This is the regression for the on-demand VPS path where
        ``tenant_vps_provision`` succeeded but ``host_hardening`` +
        ``full_setup`` never ran.
        """
        job_type = self.env["cloud.job.type"].sudo().search([], limit=1)
        self.assertTrue(
            job_type,
            "At least one cloud.job.type must be seeded by the module.",
        )
        job = (
            self.env["cloud.job"]
            .sudo()
            .create(
                {
                    "name": "historic provision",
                    "host_id": self.host.id,
                    "job_type_id": job_type.id,
                }
            )
        )

        host_id = self.host.id
        result = self._invoke_delete(host_id)
        self.assertEqual(result, {"ok": True})

        # Host survived the call …
        survivor = self.env["cloud.host"].browse(host_id)
        self.assertTrue(survivor.exists())
        # … and is archived (active=False), matching the deployed
        # branch's semantics so the SPA hides it from the host list.
        self.assertFalse(survivor.active)
        # Job history is preserved verbatim — the whole point of the
        # archive path over the FK-violating unlink path.
        self.assertTrue(job.exists())
        self.assertEqual(job.host_id, survivor)


class TestSshReadyDomain(TransactionCase):
    """``_ssh_ready_domain`` is the shared gate that ``cron_collect_metrics``
    and the prune cron use to skip hosts whose SSH layer cannot connect yet
    (no captured host key, missing credentials for the declared login_type).
    Without this filter the crons would spawn jobs that fail synchronously
    in ``ssh_connect_kwargs()`` and pollute the operator's log feed."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    def _create(self, **kw):
        # ``password`` is required on cloud.host, so every host has one even
        # when login_type='ssh_key'. We blank it explicitly per-test for the
        # password-path cases via SQL since the ORM field is required.
        return self.Host.create(
            {
                "name": kw.pop("name", "SSH Ready Probe"),
                "ip_address": "10.0.0.42",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
                "password": "pw-default",
                "known_hosts_key": "ssh-ed25519 AAAA... fake",
                "login_type": "ssh_key",
                "key_file": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n",
            }
            | kw
        )

    def _search_ready(self, *extra_domain):
        domain = self.Host._ssh_ready_domain()
        if extra_domain:
            domain = list(domain) + list(extra_domain)
        return self.Host.search(domain)

    def test_complete_ssh_key_host_is_ready(self):
        host = self._create(name="Complete SSH Key")
        self.assertIn(host, self._search_ready(("name", "=", "Complete SSH Key")))

    def test_complete_password_host_is_ready(self):
        host = self._create(
            name="Complete Password", login_type="password", key_file=False
        )
        self.assertIn(host, self._search_ready(("name", "=", "Complete Password")))

    def test_missing_known_hosts_key_is_excluded(self):
        host = self._create(name="Untrusted Host", known_hosts_key=False)
        self.assertNotIn(host, self._search_ready(("name", "=", "Untrusted Host")))

    def test_ssh_key_host_without_key_file_is_excluded(self):
        host = self._create(name="SSH Key Missing Key", key_file=False)
        self.assertNotIn(
            host,
            self._search_ready(("name", "=", "SSH Key Missing Key")),
        )


class TestSshConnectKwargsTimeouts(TransactionCase):
    """A dead/unreachable host must fail fast instead of hanging a job
    forever — ``connect_timeout``/``login_timeout`` bound both phases
    of ``asyncssh.connect()``."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    def _create(self, **kw):
        return self.Host.create(
            {
                "name": "Timeout Probe",
                "ip_address": "10.0.0.42",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
                "password": "pw-default",
                "known_hosts_key": "ssh-ed25519 AAAA... fake",
                "login_type": "password",
            }
            | kw
        )

    def test_connect_kwargs_sets_timeouts(self):
        kwargs = self._create().ssh_connect_kwargs()
        self.assertEqual(kwargs["connect_timeout"], 30)
        self.assertEqual(kwargs["login_timeout"], 30)

    # NOTE: ``ip_address``, ``user`` and ``password`` are required=True on
    # cloud.host (NOT NULL at the DB level). Their corresponding clauses
    # in ``_ssh_ready_domain`` (``('ip_address', '!=', False)`` etc.) are
    # defensive belt-and-suspenders against a future field that drops
    # required=True or a row inserted out-of-band — there is no way to
    # construct a recordset that violates these constraints from a test
    # without raising at create/write/SQL time, so they are not covered
    # here.


class TestBuildPortForwardCmd(TransactionCase):
    """The ssh -L local port-forward primitive used by the Traefik panel."""

    def _host(self, **kw):
        return self.env["cloud.host"].create(
            {
                "name": "pf-host",
                "ip_address": "10.0.0.42",
                "user": "root",
                "wildcard_domain": "example.com",
            }
            | kw
        )

    def test_builds_a_loopback_forward_on_the_default_port(self):
        cmd = self._host().build_port_forward_cmd(8080)
        self.assertEqual(
            cmd, "ssh -N -L 8080:127.0.0.1:8080 root@10.0.0.42",
        )

    def test_includes_a_non_default_ssh_port(self):
        cmd = self._host(port=2222).build_port_forward_cmd(8080)
        self.assertIn("-p 2222", cmd)

    def test_remote_host_and_port_can_differ(self):
        cmd = self._host().build_port_forward_cmd(
            9000, remote_host="10.1.2.3", remote_port=5432,
        )
        self.assertIn("-L 9000:10.1.2.3:5432", cmd)

    def test_a_host_without_an_ip_is_refused(self):
        from odoo.exceptions import UserError

        host = self._host()
        # required=True at the ORM layer, so blank it out of band.
        self.env.cr.execute(
            "UPDATE cloud_host SET ip_address = '' WHERE id = %s", (host.id,),
        )
        host.invalidate_recordset(["ip_address"])
        with self.assertRaises(UserError):
            host.build_port_forward_cmd(8080)


class KnownHostsCase(TransactionCase):
    """Shared fixture: a trusted host carrying real key material.

    The key is generated rather than faked because one of the tests below
    hands the stored line to ``ssh-keygen``, which parses the blob.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key_body = (
            asyncssh.generate_private_key("ssh-ed25519")
            .export_public_key("openssh")
            .decode()
            .strip()
        )

    def _host(self, **kw):
        return self.env["cloud.host"].create(
            {
                "name": "kh-host",
                "ip_address": "10.0.0.7",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
                "password": "pw",
                "login_type": "password",
                "known_hosts_key": f"10.0.0.7 {self.key_body}",
            }
            | kw
        )

    def _capture(self, host, server_key=None):
        """Run ``_capture_known_host_key`` against a stubbed SSH server.

        Only the transport is stubbed: the key handed back is a real
        asyncssh key object, so the entry and its fingerprint are built
        by the same code path production uses. Pass *server_key* to make
        two captures look like two different machines.
        """
        server_key = server_key or asyncssh.generate_private_key("ssh-ed25519")
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.get_server_host_key.return_value = server_key
        # ``asyncssh.connect()`` hands back an _ACMWrapper, so the double is
        # spec'd against that: a bare MagicMock would also answer to a
        # protocol asyncssh does not implement.
        connection = MagicMock(spec=_ACMWrapper)
        connection.__aenter__.return_value = conn
        connection.__aexit__.return_value = False
        with patch.object(asyncssh, "connect", return_value=connection):
            return host._capture_known_host_key()

    def _audit_details(self, host, action):
        return self.env["cloud.audit.log"].search(
            [("host_id", "=", host.id), ("action", "=", action)],
            order="id desc",
        ).mapped("details")


class TestKnownHostsFingerprint(KnownHostsCase):
    """The fingerprint is the host's identity in a form a human can
    compare. It has to match what ``ssh-keygen -lf`` prints, or it is
    worthless for comparing against anything outside this panel."""

    def test_fingerprint_matches_ssh_keygen(self):
        """Ground truth: the same key, fingerprinted by OpenSSH's own tool."""
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen is not available in this image")
        with tempfile.NamedTemporaryFile("w", suffix=".pub") as fh:
            fh.write(self.key_body + "\n")
            fh.flush()
            printed = subprocess.run(
                ["ssh-keygen", "-lf", fh.name],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        expected = next(
            token for token in printed.split() if token.startswith("SHA256:")
        )
        self.assertEqual(self._host().known_hosts_fingerprint, expected)

    def test_fingerprint_reads_the_key_blob_not_the_key_type(self):
        """The regression this replaces: reading the wrong field yielded
        something undecodable, so the fingerprint was silently dropped."""
        host = self._host()
        self.assertTrue(host.known_hosts_fingerprint.startswith("SHA256:"))
        self.assertGreater(len(host.known_hosts_fingerprint), len("SHA256:"))

    def test_fingerprint_is_empty_without_a_key(self):
        self.assertFalse(self._host(known_hosts_key=False).known_hosts_fingerprint)

    def test_fingerprint_ignores_comments_and_blank_lines(self):
        host = self._host(
            known_hosts_key=f"# a comment\n\n10.0.0.7 {self.key_body}",
        )
        self.assertEqual(
            host.known_hosts_fingerprint, self._host().known_hosts_fingerprint,
        )

    def test_fingerprint_of_an_undecodable_entry_is_empty(self):
        host = self._host(known_hosts_key="10.0.0.7 ssh-rsa not-base64!!")
        self.assertFalse(host.known_hosts_fingerprint)

    def test_fingerprint_survives_a_relabel(self):
        """Relabelling must not disturb identity — same machine, same
        fingerprint, whatever address the key is filed under."""
        host = self._host(port=22626)
        before = host.known_hosts_fingerprint
        host._relabel_known_hosts_entry()
        self.assertEqual(host.known_hosts_fingerprint, before)


class TestKnownHostsLabel(KnownHostsCase):
    """``known_hosts_key`` feeds two SSH stacks with different lookup
    rules: asyncssh (executors, terminal) matches a bare ``ip`` entry on
    any port, while OpenSSH (every Ansible-backed job) files a
    non-default port under ``[ip]:port`` and reads the bare form as port
    22 only. A label that drifts from the endpoint therefore keeps
    working everywhere *except* under OpenSSH — which is how a trusted,
    reachable host became unreachable for the recurring prune job."""

    def test_prefix_is_bare_on_the_default_port(self):
        self.assertEqual(
            self.env["cloud.host"]._known_hosts_prefix("10.0.0.7", 22),
            "10.0.0.7",
        )

    def test_prefix_is_bracketed_on_a_rotated_port(self):
        self.assertEqual(
            self.env["cloud.host"]._known_hosts_prefix("10.0.0.7", 22626),
            "[10.0.0.7]:22626",
        )

    def test_relabel_refiles_a_bare_entry_under_the_rotated_port(self):
        host = self._host(port=22626)
        host._relabel_known_hosts_entry()
        self.assertEqual(
            host.known_hosts_key, f"[10.0.0.7]:22626 {self.key_body}",
        )

    def test_relabel_leaves_the_key_material_untouched(self):
        """Relabelling must never alter trust, only the address the key is
        filed under — otherwise it could hand a host a key nobody verified
        for it."""
        host = self._host(port=22626)
        host._relabel_known_hosts_entry()
        self.assertEqual(
            host.known_hosts_key.split(None, 1)[1], self.key_body,
        )

    def test_relabel_is_idempotent(self):
        host = self._host(
            port=22626, known_hosts_key=f"[10.0.0.7]:22626 {self.key_body}",
        )
        host._relabel_known_hosts_entry()
        host._relabel_known_hosts_entry()
        self.assertEqual(
            host.known_hosts_key, f"[10.0.0.7]:22626 {self.key_body}",
        )

    def test_relabel_restores_the_bare_form_on_the_default_port(self):
        host = self._host(
            port=22, known_hosts_key=f"[10.0.0.7]:22626 {self.key_body}",
        )
        host._relabel_known_hosts_entry()
        self.assertEqual(host.known_hosts_key, f"10.0.0.7 {self.key_body}")

    def test_relabel_follows_a_changed_ip(self):
        host = self._host(port=22626)
        host.known_hosts_key = f"[10.0.0.99]:22626 {self.key_body}"
        host._relabel_known_hosts_entry()
        self.assertEqual(
            host.known_hosts_key, f"[10.0.0.7]:22626 {self.key_body}",
        )

    def test_relabel_without_a_stored_key_is_a_noop(self):
        host = self._host(port=22626, known_hosts_key=False)
        host._relabel_known_hosts_entry()
        self.assertFalse(host.known_hosts_key)

    def _openssh_finds(self, entry, lookup):
        """Return True when ``ssh-keygen`` locates *lookup* in *entry*."""
        with tempfile.NamedTemporaryFile("w", suffix="_known_hosts") as fh:
            fh.write(entry + "\n")
            fh.flush()
            return subprocess.run(
                ["ssh-keygen", "-F", lookup, "-f", fh.name],
                capture_output=True,
                check=False,
            ).returncode == 0

    def test_openssh_only_finds_the_entry_once_it_is_relabelled(self):
        """Ground truth, asked of OpenSSH itself: its lookup rule — not
        asyncssh's laxer one — is what the Ansible-backed jobs obey, and
        it is the rule the stored label has to satisfy."""
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen is not available in this image")
        host = self._host(port=22626)
        self.assertFalse(
            self._openssh_finds(host.known_hosts_key, "[10.0.0.7]:22626"),
            "a bare label must not resolve on a rotated port — if it did, "
            "the bug this guards against could not happen",
        )
        host._relabel_known_hosts_entry()
        self.assertTrue(
            self._openssh_finds(host.known_hosts_key, "[10.0.0.7]:22626"),
        )

    def test_asyncssh_accepts_both_labels_on_the_rotated_port(self):
        """Pins *why* the stale label went unnoticed for months: the
        asyncssh-based executors and terminal match either form, so they
        kept connecting while OpenSSH refused."""
        host = self._host(port=22626)
        bare = asyncssh.import_known_hosts(host.known_hosts_key)
        host._relabel_known_hosts_entry()
        bracketed = asyncssh.import_known_hosts(host.known_hosts_key)
        for known_hosts in (bare, bracketed):
            self.assertTrue(
                known_hosts.match("10.0.0.7", "10.0.0.7", 22626)[0],
            )


class TestEndpointChangeRevokesTrust(KnownHostsCase):
    """An endpoint change drops the captured key: it was verified against
    the old address, so it says nothing about whatever answers at the new
    one. A host without a key is skipped by every cron and refuses to
    build SSH kwargs, so the revocation is announced through the alert
    channels instead of only an audit row nobody reads."""

    def _active_alerts(self, host):
        return self.env["cloud.alert"].search(
            [
                ("code", "=", "host_key_revoked"),
                ("host_id", "=", host.id),
                ("state", "=", "active"),
            ]
        )

    def test_changing_the_port_revokes_and_alerts(self):
        host = self._host()
        host.write({"port": 22626})
        self.assertFalse(host.known_hosts_key)
        alert = self._active_alerts(host)
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")

    def test_changing_the_ip_revokes_and_alerts(self):
        host = self._host()
        host.write({"ip_address": "10.0.0.8"})
        self.assertFalse(host.known_hosts_key)
        self.assertEqual(len(self._active_alerts(host)), 1)

    def test_the_alert_is_deduped_across_repeated_changes(self):
        host = self._host()
        host.write({"port": 22626})
        host.known_hosts_key = f"[10.0.0.7]:22626 {self.key_body}"
        host.write({"port": 22627})
        self.assertEqual(len(self._active_alerts(host)), 1)

    def test_a_replacement_key_in_the_same_write_keeps_trust(self):
        """A manager importing a known-good key for the new endpoint is
        not a revocation."""
        host = self._host()
        host.write(
            {
                "port": 22626,
                "known_hosts_key": f"[10.0.0.7]:22626 {self.key_body}",
            }
        )
        self.assertTrue(host.known_hosts_key)
        self.assertFalse(self._active_alerts(host))

    def test_an_untrusted_host_has_nothing_to_revoke(self):
        host = self._host(known_hosts_key=False)
        host.write({"port": 22626})
        self.assertFalse(self._active_alerts(host))

    def test_a_non_endpoint_write_keeps_trust(self):
        host = self._host()
        host.write({"name": "Renamed"})
        self.assertTrue(host.known_hosts_key)
        self.assertFalse(self._active_alerts(host))

    def test_rewriting_the_same_endpoint_is_not_a_change(self):
        host = self._host()
        host.write({"ip_address": host.ip_address, "port": host.port})
        self.assertTrue(host.known_hosts_key)
        self.assertFalse(self._active_alerts(host))

    def test_recapturing_the_key_resolves_the_alert(self):
        """Re-running TOFU is the operator action the alert asks for, so
        it has to close the alert as well as restore the key — otherwise
        the panel keeps showing a resolved incident forever."""
        host = self._host()
        host.write({"port": 22626})
        self.assertTrue(self._active_alerts(host))

        captured = self._capture(host)

        self.assertTrue(captured["entry"].startswith("[10.0.0.7]:22626 "))
        self.assertEqual(host.known_hosts_key, captured["entry"])
        self.assertFalse(self._active_alerts(host))

    def test_revoking_records_the_fingerprint_it_dropped(self):
        """Without the old fingerprint there is nothing to compare the next
        capture against, and the machine-identity question is unanswerable."""
        host = self._host()
        dropped = host.known_hosts_fingerprint
        host.write({"port": 22626})
        self.assertFalse(host.known_hosts_key)
        self.assertEqual(host.revoked_key_fingerprint, dropped)

    def test_the_alert_names_the_revoked_fingerprint(self):
        host = self._host()
        dropped = host.known_hosts_fingerprint
        host.write({"port": 22626})
        self.assertIn(dropped, self._active_alerts(host).message)

    def test_the_revocation_audit_row_carries_the_fingerprint(self):
        host = self._host()
        dropped = host.known_hosts_fingerprint
        host.write({"port": 22626})
        self.assertIn(
            dropped,
            self._audit_details(
                host, "SSH host key invalidated due to endpoint change",
            ),
        )


class TestKeyIdentityOnRecapture(KnownHostsCase):
    """Comparing the newly captured key against the one the endpoint change
    revoked is the only machine-identity check that does not depend on an
    external channel — and our provider offers none (no console output, no
    metadata carrying host keys). So the comparison has to be made, recorded
    and surfaced, not left implicit."""

    def _changed_alerts(self, host):
        return self.env["cloud.alert"].search(
            [
                ("code", "=", "host_key_changed"),
                ("host_id", "=", host.id),
                ("state", "=", "active"),
            ]
        )

    def _revoke(self, host):
        """Change the endpoint so the key is revoked, and return its key."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        entry = f"10.0.0.7 {key.export_public_key('openssh').decode().strip()}"
        host.known_hosts_key = entry
        fingerprint = host.known_hosts_fingerprint
        host.write({"port": 22626})
        self.assertEqual(host.revoked_key_fingerprint, fingerprint)
        return key, fingerprint

    def test_the_same_machine_reports_an_unchanged_key(self):
        host = self._host()
        key, fingerprint = self._revoke(host)

        captured = self._capture(host, server_key=key)

        self.assertFalse(captured["changed"])
        self.assertEqual(captured["fingerprint"], fingerprint)
        self.assertEqual(captured["previous_fingerprint"], fingerprint)
        self.assertFalse(self._changed_alerts(host))

    def test_a_different_machine_raises_a_critical_alert(self):
        host = self._host()
        _key, old_fingerprint = self._revoke(host)

        captured = self._capture(host)  # a freshly generated, different key

        self.assertTrue(captured["changed"])
        self.assertNotEqual(captured["fingerprint"], old_fingerprint)
        alert = self._changed_alerts(host)
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        # Both fingerprints, or the operator cannot tell what changed to what.
        self.assertIn(old_fingerprint, alert.message)
        self.assertIn(captured["fingerprint"], alert.message)

    def test_the_new_key_is_still_trusted_after_a_change(self):
        """The operator asked for a capture, so the key is stored; the alert
        is what makes the change impossible to miss. Refusing instead would
        strand every job on a legitimately rebuilt host."""
        host = self._host()
        self._revoke(host)
        captured = self._capture(host)
        self.assertEqual(host.known_hosts_key, captured["entry"])

    def test_the_comparison_is_consumed_once(self):
        """A stale fingerprint would make the *next* capture report a verdict
        about a machine two endpoint changes ago."""
        host = self._host()
        key, _fingerprint = self._revoke(host)
        self._capture(host, server_key=key)
        self.assertFalse(host.revoked_key_fingerprint)

        second = self._capture(host)
        self.assertFalse(second["changed"])
        self.assertFalse(second["previous_fingerprint"])

    def test_a_first_capture_has_nothing_to_compare(self):
        host = self._host(known_hosts_key=False)
        captured = self._capture(host)
        self.assertFalse(captured["changed"])
        self.assertFalse(captured["previous_fingerprint"])
        self.assertTrue(captured["fingerprint"].startswith("SHA256:"))
        self.assertFalse(self._changed_alerts(host))

    def test_each_capture_is_judged_against_the_last_trusted_key(self):
        """The comparison is always against what was trusted immediately
        before, so going back to an older key is itself a change — the alert
        tracks continuity, not a whitelist of keys ever seen."""
        host = self._host()
        original, _fingerprint = self._revoke(host)
        replacement = self._capture(host)["fingerprint"]  # rebuilt machine

        host.write({"port": 22627})
        back_to_original = self._capture(host, server_key=original)

        self.assertTrue(back_to_original["changed"])
        self.assertEqual(back_to_original["previous_fingerprint"], replacement)

    def test_a_change_alert_waits_for_a_human_to_dismiss_it(self):
        """Unlike the revocation alert, this one records that the machine's
        identity changed once — a past event, not a live condition. A routine
        re-trust must not retire it unread, or whoever caused the change could
        erase the warning by triggering one."""
        host = self._host()
        self._revoke(host)
        replacement = asyncssh.generate_private_key("ssh-ed25519")
        self._capture(host, server_key=replacement)  # different machine → alert
        self.assertTrue(self._changed_alerts(host))

        # A later capture confirming continuity: the same machine that is
        # trusted right now, so nothing is wrong any more…
        host.write({"port": 22628})
        confirmed = self._capture(host, server_key=replacement)
        self.assertFalse(confirmed["changed"])

        # …and the earlier identity change is still flagged for a human.
        self.assertTrue(self._changed_alerts(host))

    def test_the_capture_is_audited_with_its_verdict(self):
        host = self._host()
        key, _fingerprint = self._revoke(host)
        self._capture(host, server_key=key)
        details = self._audit_details(host, "SSH host key trusted")
        self.assertTrue(details)
        self.assertIn("unchanged", details[0])


class TestHardeningKeepsTheKeyReachable(KnownHostsCase):
    """Hardening rotates the SSH port of the *same* machine, so it opts
    out of the revocation above (re-running TOFU would trade a verified
    key for a blind capture) and re-files the key instead. Losing that
    relabel is what left every hardened host unreachable for the
    Ansible-backed jobs."""

    def _run_on_success(self, host):
        executor = object.__new__(HostHardeningExecutor)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.host_id = host
        executor.env = self.env
        executor._log_buffer = []
        with patch.object(HostHardeningExecutor, "_sys"), patch.object(
            HostHardeningExecutor, "_resolve_alert",
        ), patch.object(
            HostHardeningExecutor,
            "_open_edge_firewall_port",
            new=AsyncMock(return_value=None),
        ), patch.object(
            HostHardeningExecutor,
            "_finalize_disable_root",
            new=AsyncMock(return_value=True),
        ):
            asyncio.run(executor.on_success({}))
        return executor

    def test_on_success_refiles_the_key_under_the_rotated_port(self):
        host = self._host(port=22)
        executor = self._run_on_success(host)
        self.assertEqual(host.port, executor._new_port)
        self.assertEqual(
            host.known_hosts_key,
            f"[10.0.0.7]:{executor._new_port} {self.key_body}",
        )

    def test_hardening_does_not_revoke_or_alert(self):
        host = self._host(port=22)
        self._run_on_success(host)
        self.assertTrue(host.known_hosts_key)
        self.assertFalse(
            self.env["cloud.alert"].search(
                [
                    ("code", "=", "host_key_revoked"),
                    ("host_id", "=", host.id),
                ]
            )
        )
