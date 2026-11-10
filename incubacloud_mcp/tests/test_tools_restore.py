"""Restores from an archive: the Import dialog's ways, through an agent.

Each tool runs the instance's own method as the user, asks before it
runs, and keeps out of the audit, the approval and the answer what a
link carries beyond its host and its file.
"""

from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.incubacloud.net import restore_source
from odoo.addons.incubacloud.net.restore_source import told_link

from ..models.mcp_tools_restore import shown_link
from ..protocol.schema import SchemaError, validate
from .common import ConfirmMixin, McpHttpMixin, McpUsersMixin, PanelFixtureMixin

RESTORES = ("restore_from_link", "restore_from_host_file", "restore_from_upload")
#: A presigned link: its query is the credential.
SIGNED = (
    "https://93.184.216.34/bucket/acme-7/backup.zip"
    "?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=deadbeefcafe"
)
SHOWN = "https://93.184.216.34/…/backup.zip?…"
#: What the approval and the assistant's card show of it: who signed it
#: whole, as the bucket of a link a storage signed; the signature cut.
TOLD = (
    "https://93.184.216.34/bucket/acme-7/backup.zip"
    "?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=de…fe"
)
SHA = "ab" * 32


def _public_resolution(address="93.184.216.34"):
    """Have every name resolve to one public address, without DNS."""
    return patch.object(
        restore_source,
        "_resolve_public",
        autospec=True,
        return_value=address,
    )


def _caller_key():
    """Return ``(public line with a comment, private PEM)`` of a new key."""
    key = ed25519.Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return f"{public} agent@laptop", private


class RestoreToolsMixin:
    """Prepare, confirm and run a tool as a call does, without HTTP."""

    def _tools(self, user):
        """The handlers as *user*."""
        return self.env["incubacloud.mcp.tools"].with_user(user)

    def _spec(self, name):
        """The tool's spec."""
        return self.env["incubacloud.mcp.catalog"]._spec(name)

    def _prepared(self, user, name, args):
        """*args* validated and prepared as ``_tools_call`` does."""
        args = validate(self._spec(name).input_schema, args)
        prepare = getattr(self._tools(user), f"_prepare_{name}", None)
        return prepare(args) if prepare else args

    def _message(self, user, name, args):
        """What *user* is asked to approve for *name*."""
        args = self._prepared(user, name, args)
        return self._tools(user)._confirmation(self._spec(name), args)

    def _run(self, user, name, args):
        """Run *name* once approved; the job's state is left out."""
        args = self._prepared(user, name, args)
        Tools = type(self.env["incubacloud.mcp.tools"])
        with patch.object(
            Tools,
            "_with_job_state",
            autospec=True,
            side_effect=lambda _tools, result: result,
        ):
            return self._tools(user)._invoke(self._spec(name), args)


@tagged("post_install", "-at_install")
class TestRestoreTools(
    RestoreToolsMixin, PanelFixtureMixin, McpUsersMixin, TransactionCase
):
    """Who may restore, what is asked, and what each tool calls."""

    def setUp(self):
        """A developer and a consultant of the project; production has a
        backup destination, so its safety backup can be taken."""
        super().setUp()
        self._panel_fixture()
        self.dev = self._cloud_user("mcp-rst-dev")
        self.consultant = self._cloud_user(
            "mcp-rst-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.dev.id), (4, self.consultant.id)]
        self.production.sudo().custom_backup_dst = "s3://mcp-bucket/acme/prod"

    def _jobs(self):
        """How many jobs the panel holds."""
        return self.env["cloud.job"].sudo().search_count([])

    def _checked_grant(self, instance=None):
        """A door of *instance* whose upload was checked."""
        grant, _key = self.env["cloud.restore.upload.grant"]._open(
            instance or self.staging
        )
        grant.write(
            {
                "received_filename": "restore.zip",
                "received_bytes": 5,
                "received_sha256": SHA,
            }
        )
        return grant

    # ── Declarations ──────────────────────────────────────────────────────

    def test_every_restore_is_destructive_and_always_asks(self):
        """The three restores are destructive and always ask, like
        restore_backup; opening a door asks, checking it does not."""
        for name in RESTORES:
            spec = self._spec(name)
            self.assertEqual(
                (spec.destructive, spec.confirm, spec.min_group),
                (True, "always", "group_cloud_developer"),
                name,
            )
            self.assertIn("dump.sql", spec.description)
            self.assertIn("filestore/", spec.description)
            self.assertIn("lost", spec.description)
        self.assertEqual(self._spec("grant_restore_upload").confirm, "always")
        self.assertEqual(self._spec("check_restore_upload").confirm, "never")
        revoke = self._spec("revoke_restore_upload")
        self.assertEqual(
            (revoke.destructive, revoke.read_only, revoke.confirm, revoke.min_group),
            (False, False, "never", "group_cloud_developer"),
        )

    def test_the_browser_upload_stays_out(self):
        """The routes of the browser's upload are still excluded, and no
        new route of the panel became a tool."""
        policies = self.env["incubacloud.mcp.catalog"]._route_policies()
        for route in (
            "/cloud/instance/<int:instance_id>/restore",
            "/cloud/instance/<int:instance_id>/restore/begin",
            "/cloud/instance/<int:instance_id>/restore/part",
            "/cloud/instance/<int:instance_id>/restore/finish",
        ):
            self.assertEqual(policies[route][0], "excluded", route)
        for name in (
            *RESTORES,
            "grant_restore_upload",
            "check_restore_upload",
            "revoke_restore_upload",
        ):
            self.assertFalse(self._spec(name).route, name)
            self.assertNotIn(("tool", name), policies.values())

    # ── Who ───────────────────────────────────────────────────────────────

    def test_a_role_below_developer_is_refused_before_being_asked(self):
        """The dialog's role: a consultant is refused by every tool."""
        cases = {
            "restore_from_link": {"instance_id": self.staging.id, "url": SIGNED},
            "restore_from_host_file": {"instance_id": self.staging.id},
            "grant_restore_upload": {
                "instance_id": self.staging.id,
                "public_key": _caller_key()[0],
            },
        }
        for name, args in cases.items():
            with self.subTest(name), self.assertRaises(AccessError):
                self._message(self.consultant, name, args)
        before = self._jobs()
        with self.assertRaises(AccessError):
            self._run(
                self.consultant, "restore_from_host_file", {"instance_id": self.staging.id}
            )
        self.assertEqual(self._jobs(), before)

    def test_an_instance_the_user_may_not_read_is_refused(self):
        """No ``sudo``: a record rule that hides the instance from the
        user refuses the approval and the run, and nothing is queued."""
        self.env["ir.rule"].create(
            {
                "name": "mcp-rst: hide the staging",
                "model_id": self.env.ref("incubacloud.model_cloud_instance").id,
                "domain_force": f"[('id', '!=', {self.staging.id})]",
            }
        )
        before = self._jobs()
        for name, args in (
            ("restore_from_host_file", {"instance_id": self.staging.id}),
            ("restore_from_link", {"instance_id": self.staging.id, "url": SIGNED}),
        ):
            with self.subTest(name):
                with self.assertRaises(AccessError):
                    self._message(self.dev, name, args)
                with self.assertRaises(AccessError):
                    self._run(self.dev, name, args)
        self.assertEqual(self._jobs(), before)

    # ── From a link ───────────────────────────────────────────────────────

    def test_an_agent_s_link_is_https_without_credentials(self):
        """sftp, ftp, http, a file and a link with a user or a password are
        refused before anyone is asked, and the password is not quoted."""
        for url in (
            "sftp://93.184.216.34/backup.zip",
            "ftp://93.184.216.34/backup.zip",
            "http://93.184.216.34/backup.zip",
            "file:///etc/passwd",
            "https://admin:hunter2-secret@93.184.216.34/backup.zip",
            "https://token-only@93.184.216.34/backup.zip",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UserError) as caught:
                    self._message(
                        self.dev,
                        "restore_from_link",
                        {"instance_id": self.staging.id, "url": url},
                    )
                self.assertNotIn("hunter2", str(caught.exception))

    def test_a_link_to_the_inside_is_refused_before_being_asked(self):
        """Loopback, private, link-local (the cloud's metadata) and a name
        that resolves to one: the shared check refuses each."""
        for url in (
            "https://127.0.0.1/backup.zip",
            "https://10.0.0.5/backup.zip",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/backup.zip",
            "https://[::ffff:127.0.0.1]/backup.zip",
        ):
            with self.subTest(url=url), self.assertRaises(UserError):
                self._message(
                    self.dev,
                    "restore_from_link",
                    {"instance_id": self.staging.id, "url": url},
                )
        with (
            patch.object(
                restore_source,
                "_resolve_public",
                autospec=True,
                side_effect=restore_source.OutboundError(
                    "'rebind.example' resolves to a private address"
                ),
            ),
            self.assertRaisesRegex(UserError, "private"),
        ):
            self._message(
                self.dev,
                "restore_from_link",
                {"instance_id": self.staging.id, "url": "https://rebind.example/b.zip"},
            )

    def test_the_approval_shows_every_piece_of_the_link_cut(self):
        """Each segment and each value of the query, cut (``told_link``):
        the signature is not in the approval whole; who signed it is, and
        the options and the instance."""
        with _public_resolution():
            message = self._message(
                self.dev,
                "restore_from_link",
                {"instance_id": self.staging.id, "url": SIGNED},
            )
        self.assertIn(TOLD, message)
        self.assertNotIn("deadbeefcafe", message)
        self.assertIn("X-Amz-Credential=AKIAEXAMPLE&", message)
        self.assertIn('"mcp-acme-staging"', message)
        self.assertIn("neutralized", message)
        self.assertIn("without a safety backup", message)
        self.assertIn("lost", message)

    def test_a_look_alike_link_does_not_read_the_same_in_the_approval(self):
        """Same host and file's name, another bucket's path or another
        share's token: the audit's form would read the same, the
        approval's and the card's do not."""
        Tools = self.env["incubacloud.mcp.tools"]
        spec = self._spec("restore_from_link")
        share = "Z8k2LmQ9pR4tW7xYbC3d"
        for mine, theirs in (
            (
                SIGNED,
                SIGNED.replace("bucket/acme-7", "attacker/acme-7"),
            ),
            (
                f"https://cloud.example.com/s/{share}/download",
                "https://cloud.example.com/s/Qw3rTy7uIo1pAs5dFg9h/download",
            ),
        ):
            with self.subTest(mine=mine), _public_resolution():
                told = [
                    self._message(
                        self.dev,
                        "restore_from_link",
                        {"instance_id": self.staging.id, "url": url},
                    )
                    for url in (mine, theirs)
                ]
                shown = [
                    Tools._shown_args(spec, {"url": url})["url"] for url in (mine, theirs)
                ]
                self.assertEqual(shown_link(mine), shown_link(theirs))
                self.assertNotEqual(*told)
                self.assertNotEqual(*shown)
                self.assertNotIn(share, told[0] + shown[0])

    def test_the_audit_keeps_the_link_s_host_and_file_only(self):
        """What the audit records of the call: host and file's name, cut."""
        args = self._prepared(
            self.dev, "restore_from_link", {"instance_id": self.staging.id, "url": SIGNED}
        )
        recorded = self.env["incubacloud.mcp.tools"]._recorded_args(
            self._spec("restore_from_link"), args
        )
        self.assertEqual(recorded["url"], SHOWN)
        self.assertEqual(args["url"], SIGNED)

    def test_a_token_that_ends_the_link_is_not_kept_whole(self):
        """``we.tl/t-…``: neither the audit nor the answer keeps the token."""
        share = "t-AbCdEfGhIjKl"
        url = f"https://we.tl/{share}"
        args = self._prepared(
            self.dev, "restore_from_link", {"instance_id": self.staging.id, "url": url}
        )
        recorded = self.env["incubacloud.mcp.tools"]._recorded_args(
            self._spec("restore_from_link"), args
        )
        self.assertNotIn(share, recorded["url"])
        with _public_resolution():
            answer = self._run(
                self.dev,
                "restore_from_link",
                {"instance_id": self.staging.id, "url": url},
            )
        self.assertNotIn(share, str(answer))
        job = self.env["cloud.job"].sudo().browse(answer["job_id"])
        self.assertNotIn(share, str(job.payload))
        self.assertEqual(job._restore_url_link(), url)

    def test_an_agent_s_link_keeps_to_port_443_and_off_the_panel_s_servers(self):
        """Another port, a server the panel manages, the panel itself, and
        the IPv6 ranges Python calls global: refused before anyone is
        asked, nothing queued."""
        public_host = (
            self.env["cloud.host"]
            .sudo()
            .create(
                {
                    "name": "mcp-rst-public-host",
                    "ip_address": "93.184.216.50",
                    "user": "root",
                    "wildcard_domain": "mcp-rst-public.example",
                }
            )
        )
        before = self._jobs()
        CloudHost = type(self.env["cloud.host"])
        for url, address, reason in (
            ("https://files.example.com:8443/b.zip", "93.184.216.34", "port 443"),
            ("https://files.example.com/b.zip", public_host.ip_address, "server"),
            ("https://files.example.com/b.zip", "93.184.216.51", "server"),
        ):
            with (
                self.subTest(url=url, address=address),
                _public_resolution(address),
                patch.object(
                    CloudHost,
                    "_hardening_panel_ip",
                    autospec=True,
                    return_value="93.184.216.51",
                ),
                self.assertRaisesRegex(UserError, reason),
            ):
                self._message(
                    self.dev,
                    "restore_from_link",
                    {"instance_id": self.staging.id, "url": url},
                )
        for url in (
            "https://[64:ff9b::7f00:1]/b.zip",
            "https://[::7f00:1]/b.zip",
            "https://[fec0::1]/b.zip",
        ):
            with self.subTest(url=url), self.assertRaises(UserError):
                self._message(
                    self.dev,
                    "restore_from_link",
                    {"instance_id": self.staging.id, "url": url},
                )
        self.assertEqual(self._jobs(), before)

    def test_the_options_default_as_the_kind_of_instance_wants(self):
        """A staging is neutralized by default, production is not; both get
        their own address back unless told otherwise; the call is bound to
        what was decided."""
        staging = self._prepared(
            self.dev, "restore_from_host_file", {"instance_id": self.staging.id}
        )
        production = self._prepared(
            self.dev, "restore_from_host_file", {"instance_id": self.production.id}
        )
        told = self._prepared(
            self.dev,
            "restore_from_host_file",
            {
                "instance_id": self.staging.id,
                "neutralize": False,
                "reset_base_url": False,
            },
        )
        self.assertEqual(
            (staging["neutralize"], staging["reset_base_url"]), (True, True)
        )
        self.assertEqual(
            (production["neutralize"], production["reset_base_url"]), (False, True)
        )
        self.assertEqual((told["neutralize"], told["reset_base_url"]), (False, False))

    def test_a_link_restore_calls_restore_from_url_with_the_options(self):
        """The dialog's method, as the user, with every option; the answer
        shows the link as the audit keeps it."""
        Instance = type(self.env["cloud.instance"])
        with (
            _public_resolution(),
            patch.object(
                Instance, "restore_from_url", autospec=True, return_value=91
            ) as restore,
        ):
            answer = self._run(
                self.dev,
                "restore_from_link",
                {"instance_id": self.staging.id, "url": SIGNED},
            )
        self.assertEqual(answer, {"ok": True, "job_id": 91, "link": SHOWN})
        restore.assert_called_once()
        called, url = restore.call_args.args
        self.assertEqual((called, called.env.uid), (self.staging, self.dev.id))
        self.assertEqual(url, SIGNED)
        self.assertEqual(
            restore.call_args.kwargs,
            {
                "backup_before_restore": False,
                "neutralize": True,
                "reset_base_url": True,
                "from_agent": True,
            },
        )

    def test_a_link_restore_of_production_takes_its_safety_backup_first(self):
        """Production: the safety backup is the chain's first job, whatever
        the call says, and the approval says so; without a destination the
        restore is refused before anyone is asked."""
        args = {
            "instance_id": self.production.id,
            "url": SIGNED,
            "backup_before_restore": False,
        }
        with _public_resolution():
            self.assertIn(
                "after a safety backup", self._message(self.dev, "restore_from_link", args)
            )
            self.assertIn(
                "production instance", self._message(self.dev, "restore_from_link", args)
            )
            answer = self._run(self.dev, "restore_from_link", args)
        first = self.env["cloud.job"].sudo().browse(answer["job_id"])
        self.assertEqual(first.job_type_id.code, "backup_create")
        restore = (
            self.env["cloud.job"]
            .sudo()
            .search(
                [
                    ("instance_id", "=", self.production.id),
                    ("job_type_id.code", "=", "restore_instance"),
                ]
            )
        )
        self.assertEqual(len(restore), 1)
        self.assertEqual(restore.payload["mode"], "from_url")
        self.assertEqual(restore._restore_url_link(), SIGNED)
        self.assertNotIn("deadbeefcafe", str(restore.payload))
        self.assertFalse(first.secret_payload)
        self.assertEqual(
            (restore.payload["neutralize"], restore.payload["reset_base_url"]),
            (False, True),
        )
        self.production.sudo().custom_backup_dst = False
        with _public_resolution(), self.assertRaisesRegex(UserError, "safety backup"):
            self._message(self.dev, "restore_from_link", args)

    # ── From a file on the server ─────────────────────────────────────────

    def test_a_host_file_restore_takes_no_path(self):
        """The one path the restore reads is fixed per instance: a path from
        the agent is not even accepted."""
        for extra in (
            {"path": "/etc/shadow"},
            {"path": "../../root/.ssh/id_rsa"},
            {"local_path": "/etc/odoo/odoo.conf"},
            {"mode": "browser"},
        ):
            with self.subTest(extra=extra), self.assertRaises(SchemaError):
                self._prepared(
                    self.dev,
                    "restore_from_host_file",
                    {"instance_id": self.staging.id, **extra},
                )
        message = self._message(
            self.dev, "restore_from_host_file", {"instance_id": self.staging.id}
        )
        self.assertIn(f'"/tmp/incubacloud-restore-{self.staging.id}.zip"', message)
        self.assertIn('"mcp-host"', message)

    def test_a_host_file_restore_is_restore_db_in_rsync_mode(self):
        """Only the mode and the options reach the payload."""
        Instance = type(self.env["cloud.instance"])
        with patch.object(
            Instance, "restore_db", autospec=True, return_value=92
        ) as restore:
            answer = self._run(
                self.dev,
                "restore_from_host_file",
                {"instance_id": self.staging.id, "backup_before_restore": True},
            )
        self.assertEqual(answer["job_id"], 92)
        self.assertEqual(
            answer["path"], f"/tmp/incubacloud-restore-{self.staging.id}.zip"
        )
        called, payload = restore.call_args.args
        self.assertEqual((called, called.env.uid), (self.staging, self.dev.id))
        self.assertEqual(
            payload,
            {
                "mode": "rsync",
                "backup_before_restore": True,
                "neutralize": True,
                "reset_base_url": True,
            },
        )

    # ── Through an upload door ────────────────────────────────────────────

    def test_a_door_opens_for_the_agent_s_public_key_and_hands_out_no_private_one(
        self,
    ):
        """The approval names the key and the deadline; the door installs
        the agent's key, without its comment; the answer carries what
        sending needs and no private key."""
        public, private = _caller_key()
        args = {"instance_id": self.staging.id, "public_key": public}
        message = self._message(self.dev, "grant_restore_upload", args)
        self.assertIn("SHA256:", message)
        self.assertIn("12 hours", message)
        self.assertIn('"mcp-host"', message)
        answer = self._run(self.dev, "grant_restore_upload", args)
        grant = self.env["cloud.restore.upload.grant"].browse(answer["grant_id"])
        self.assertEqual(grant.public_key, public.rsplit(" ", 1)[0])
        self.assertEqual(grant.user_id, self.dev)
        self.assertNotIn("private_key", answer)
        text = str(answer)
        self.assertNotIn("PRIVATE KEY", text)
        self.assertNotIn(private.splitlines()[1], text)
        self.assertNotIn(grant.token, text)
        self.assertIn("root@10.0.0.90:restore.zip", answer["upload_command"])
        install = self.env["cloud.job"].sudo().browse(answer["job_id"])
        self.assertEqual(install.job_type_id.code, "grant_restore_upload_key")
        self.assertTrue(
            self.env["cloud.audit.log"]
            .sudo()
            .search(
                [
                    ("action", "=", "Granted a temporary restore upload key"),
                    ("instance_id", "=", self.staging.id),
                ]
            )
        )

    def test_a_private_key_given_by_mistake_is_refused_and_not_quoted(self):
        """Refused while the call is prepared, before anything is recorded."""
        _public, private = _caller_key()
        with self.assertRaisesRegex(UserError, "ed25519") as caught:
            self._prepared(
                self.dev,
                "grant_restore_upload",
                {"instance_id": self.staging.id, "public_key": private[:1024]},
            )
        self.assertNotIn(private.splitlines()[1], str(caught.exception))

    def test_a_key_opens_one_door_only(self):
        """The same key twice: the second door is refused."""
        public, _private = _caller_key()
        args = {"instance_id": self.staging.id, "public_key": public}
        self._run(self.dev, "grant_restore_upload", args)
        with self.assertRaisesRegex(UserError, "already opened"):
            self._run(self.dev, "grant_restore_upload", args)

    def test_a_door_is_closed_without_approval_and_audited(self):
        """``revoke_restore_upload`` as the panel's: the key's removal and
        the directory's queued, the grant closed; nothing to approve; a
        closed door says so; a door of another instance is not this one's."""
        grant = self._checked_grant()
        args = {"instance_id": self.staging.id, "grant_id": grant.id}
        self.assertIsNone(
            self._tools(self.dev)._confirmation(self._spec("revoke_restore_upload"), args)
        )
        answer = self._run(self.dev, "revoke_restore_upload", args)
        self.assertEqual((answer["ok"], answer["closed"]), (True, True))
        job = self.env["cloud.job"].sudo().browse(answer["job_id"])
        self.assertEqual(job.job_type_id.code, "revoke_restore_upload_key")
        self.assertNotIn("keep_directory", job.payload)
        self.assertEqual(grant.state, "revoked")
        again = self._run(self.dev, "revoke_restore_upload", args)
        self.assertEqual((again["ok"], again["closed"]), (True, False))
        self.assertTrue(
            self._tools(self.dev)._writes(self._spec("revoke_restore_upload"), args)
        )
        with self.assertRaisesRegex(UserError, "does not exist"):
            self._run(
                self.dev,
                "revoke_restore_upload",
                {"instance_id": self.production.id, "grant_id": grant.id},
            )
        with self.assertRaises(AccessError):
            self._run(self.consultant, "revoke_restore_upload", args)

    def test_checking_a_door_queues_the_panel_s_check(self):
        """``verify_restore_upload``; a door of another instance is not this
        one's."""
        grant = self._checked_grant()
        answer = self._run(
            self.dev,
            "check_restore_upload",
            {"instance_id": self.staging.id, "grant_id": grant.id},
        )
        job = self.env["cloud.job"].sudo().browse(answer["job_id"])
        self.assertEqual(job.job_type_id.code, "verify_restore_upload")
        with self.assertRaisesRegex(UserError, "does not exist"):
            self._run(
                self.dev,
                "check_restore_upload",
                {"instance_id": self.production.id, "grant_id": grant.id},
            )

    def test_nothing_checked_is_not_restored(self):
        """A door nothing was found through is refused before anyone is asked."""
        grant, _key = self.env["cloud.restore.upload.grant"]._open(self.staging)
        with self.assertRaisesRegex(UserError, "check_restore_upload"):
            self._message(
                self.dev,
                "restore_from_upload",
                {"instance_id": self.staging.id, "grant_id": grant.id},
            )

    def test_the_approval_names_the_file_and_binds_the_call_to_it(self):
        """The file the check found, by name, size and digest; another digest
        is refused before anyone is asked."""
        grant = self._checked_grant()
        args = {"instance_id": self.staging.id, "grant_id": grant.id}
        prepared = self._prepared(self.dev, "restore_from_upload", args)
        self.assertEqual(prepared["expected_sha256"], SHA)
        message = self._message(self.dev, "restore_from_upload", args)
        self.assertIn('"restore.zip"', message)
        self.assertIn(SHA, message)
        self.assertIn("5 bytes", message)
        for other in ("cd" * 32, "not-a-digest"):
            with self.subTest(other=other), self.assertRaises(UserError):
                self._message(
                    self.dev,
                    "restore_from_upload",
                    {**args, "expected_sha256": other},
                )

    def test_an_upload_restore_is_restore_db_in_ssh_upload_mode(self):
        """The grant, the digest shown and the options: nothing else."""
        grant = self._checked_grant()
        Instance = type(self.env["cloud.instance"])
        with patch.object(
            Instance, "restore_db", autospec=True, return_value=93
        ) as restore:
            answer = self._run(
                self.dev,
                "restore_from_upload",
                {"instance_id": self.staging.id, "grant_id": grant.id},
            )
        self.assertEqual(answer, {"ok": True, "job_id": 93})
        called, payload = restore.call_args.args
        self.assertEqual((called, called.env.uid), (self.staging, self.dev.id))
        self.assertEqual(
            payload,
            {
                "mode": "ssh_upload",
                "grant_id": grant.id,
                "expected_sha256": SHA,
                "backup_before_restore": False,
                "neutralize": True,
                "reset_base_url": True,
            },
        )

    def test_a_door_of_another_instance_restores_nothing(self):
        """The door must be the instance's own."""
        grant = self._checked_grant(self.production)
        with self.assertRaisesRegex(UserError, "does not exist"):
            self._message(
                self.dev,
                "restore_from_upload",
                {"instance_id": self.staging.id, "grant_id": grant.id},
            )

    def test_shown_link(self):
        """Host and file's name; the path before it, the query, any user."""
        for url, shown in (
            (SIGNED, SHOWN),
            ("https://files.example.com/backup.zip", "https://files.example.com/backup.zip"),
            ("https://files.example.com:8443/a/b/c.zip#x", "https://files.example.com:8443/…/c.zip"),
            ("https://u:p@files.example.com/s/tok/d.zip?dl=1", "https://files.example.com/…/d.zip?…"),
            ("https://files.example.com", "https://files.example.com/"),
        ):
            with self.subTest(url=url):
                self.assertEqual(shown_link(url), shown)


@tagged("post_install", "-at_install")
class TestRestoreToolsOverHttp(
    ConfirmMixin, PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """As an agent calls them: approved first, audited after, as the user."""

    def setUp(self):
        """A developer's agent on the project, and a consultant's."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.dev = self._cloud_user("mcp-rst-http-dev")
        self.consultant = self._cloud_user(
            "mcp-rst-http-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.dev.id), (4, self.consultant.id)]
        self.key = self._api_key(self.dev)

    def _restores(self):
        """The restore jobs of the staging."""
        return (
            self.env["cloud.job"]
            .sudo()
            .search(
                [
                    ("instance_id", "=", self.staging.id),
                    ("job_type_id.code", "=", "restore_instance"),
                ]
            )
        )

    def test_a_consultant_s_agent_neither_sees_nor_runs_them(self):
        """Not listed, and refused before anything runs."""
        credential = self._api_key(self.consultant)
        for name in (
            *RESTORES,
            "grant_restore_upload",
            "check_restore_upload",
            "revoke_restore_upload",
        ):
            with self.subTest(name):
                self.assertNotIn(name, self._tool_names(credential))
        result = self._call_tool(
            credential, "restore_from_host_file", {"instance_id": self.staging.id}
        )
        self.assertTrue(result["isError"])
        self.assertFalse(self._restores())

    def test_a_restore_waits_for_the_user_and_is_audited_without_the_signature(self):
        """Nothing is queued until the user approves; then the restore
        runs as the dialog's, and the audit row, the approval and the
        answer keep the link's host and file only."""
        arguments = {"instance_id": self.staging.id, "url": SIGNED}
        with _public_resolution():
            first = self._call_tool(
                self.key, "restore_from_link", arguments, {"elicitation": {"form": {}}}
            )
            self.assertEqual(first["resultType"], "input_required")
            self.assertFalse(self._restores())
            message = first["inputRequests"]["confirm"]["params"]["message"]
            self.assertIn(TOLD, message)
            self.assertNotIn("deadbeefcafe", message)
            result = self._confirmed_call(self.key, "restore_from_link", arguments)
        self.assertFalse(result.get("isError"), result)
        answer = self._payload(result)
        self.assertEqual(answer["link"], SHOWN)
        self.assertNotIn("deadbeefcafe", str(result))
        job = self._restores()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.create_uid, self.dev)
        self.assertEqual(
            (job.payload["mode"], job.payload["neutralize"], job.payload["reset_base_url"]),
            ("from_url", True, True),
        )
        self.assertEqual(job.payload["link_policy"], "agent")
        self.assertNotIn("deadbeefcafe", str(job.payload))
        row = (
            self.env["cloud.audit.log"]
            .sudo()
            .search([("action", "=", "Agent: restore_from_link")])
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row.instance_id, self.staging)
        self.assertEqual(row.user_id, self.dev)
        self.assertIn("(test-agent)", row.via_client)
        self.assertIn(SHOWN, row.details)
        for hidden in ("deadbeefcafe", "AKIAEXAMPLE", "acme-7"):
            self.assertNotIn(hidden, row.details)
        approval = (
            self.env["incubacloud.mcp.approval"]
            .sudo()
            .search([("tool", "=", "restore_from_link")])
        )
        self.assertTrue(approval)
        for each in approval:
            self.assertNotIn("deadbeefcafe", each.message)

    def test_a_restore_of_nothing_checked_is_refused_before_being_asked(self):
        """A door nothing was found through: refused, nobody asked, and
        nothing queued."""
        grant, _key = self.env["cloud.restore.upload.grant"]._open(self.staging)
        result = self._call_tool(
            self.key,
            "restore_from_upload",
            {"instance_id": self.staging.id, "grant_id": grant.id},
        )
        self.assertTrue(result["isError"])
        self.assertIn("check_restore_upload", result["content"][0]["text"])
        self.assertNotEqual(result.get("resultType"), "input_required")
        self.assertFalse(self._restores())
