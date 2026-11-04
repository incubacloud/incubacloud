"""Restores from an archive the panel did not take: the Import dialog's ways.

The panel imports a database four ways: an upload from the browser, a
link the server downloads, a file already on the server, and an upload
through a one-use SSH door. An agent gets the last three; the browser's
upload stays in the panel (``CORE_EXCLUDED``). Each tool calls the very
method of the instance the dialog calls, as the user — the same role,
the same record rules, the same executor: the safety backup production
always takes, the neutralization, the instance's own address, and the
panel's connector ensured.

What the agent names is never trusted further than the dialog trusts
it. A link is checked and pinned by ``restore_from_url``, and an agent's
must be ``https`` on port 443 without a user or a password (decision 6:
credentials are written in the panel only), and name no server of the
panel's (``from_agent``). The job keeps it encrypted. The audit and the
answer keep its host and its file's name, cut (``shown_link``); the
approval and the assistant's card every piece of it, cut
(``told_link``): two links of one host read differently there, and
neither form suffices to download. A file on the server is read from one
path only, fixed per instance, never one the agent gives. A door installs
the agent's own public key, never one the panel makes: no private key
passes through an agent.
"""

import re
from urllib.parse import urlsplit

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.cloud_restore_upload_grant import GRANT_HOURS
from odoo.addons.incubacloud.models.restore_instance_executor import rsync_path
from odoo.addons.incubacloud.net.restore_source import shown_link, told_link

from ..protocol.tool_spec import ToolSpec, boolean, obj, record_id, string
from .mcp_tools import quoted

_DEVELOPER = "group_cloud_developer"

_INSTANCE = record_id("The instance whose database and files are replaced.")
_GRANT = record_id("The upload door (grant_id from grant_restore_upload).")

#: What every restore says of its archive, and of what it replaces.
_ARCHIVE = (
    "The archive is a .zip as the panel's Import takes it, the format of "
    "Odoo's own backups: dump.sql (a plain SQL dump of the database) and the "
    "filestore/ directory at its root. It replaces the instance's database and "
    "files: what the instance holds now is lost."
)
#: The options every restore takes, as the dialog and the executor name them.
_OPTIONS = {
    "neutralize": boolean(
        "Restore a neutralized copy: scheduled actions and outgoing mail off. "
        "True by default except on production."
    ),
    "reset_base_url": boolean(
        "Give the restored database this instance's own address (web.base.url). "
        "True by default."
    ),
    "backup_before_restore": boolean(
        "Take a safety backup first. Production always takes one, whatever this "
        "says."
    ),
}
#: Answered by every restore.
_ANSWER = (
    " Answers the first job_id of the chain (the safety backup, when one is "
    "taken); follow it with get_job."
)

#: A SHA-256 as ``sha256sum`` prints it.
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _restore(name, description, schema, required):
    """Return the spec of a restore tool: destructive, always approved."""
    return ToolSpec(
        name,
        f"{description} {_ARCHIVE}{_ANSWER}",
        _DEVELOPER,
        obj({"instance_id": _INSTANCE} | schema | _OPTIONS, required=required),
        handler=f"_tool_{name}",
        destructive=True,
        confirm="always",
    )


TOOLS = (
    _restore(
        "restore_from_link",
        "Replace an instance's database and files with an archive its server "
        "downloads from an https link (a presigned or share link). Links with a "
        "user and password, and sftp or ftp ones, are given in the panel (Import, "
        "From a link).",
        {"url": string("The archive's https link.", max_length=4096)},
        ("instance_id", "url"),
    ),
    _restore(
        "restore_from_host_file",
        "Replace an instance's database and files with an archive already on its "
        "server, at /tmp/incubacloud-restore-<instance_id>.zip: a regular file of "
        "the account the panel connects as (copy it there with run_host_command, "
        "for instance). No other path is read; the file is taken by the restore, "
        "and put back if the restore fails.",
        {},
        ("instance_id",),
    ),
    _restore(
        "restore_from_upload",
        "Replace an instance's database and files with the archive sent through "
        "an upload door (grant_restore_upload, then check_restore_upload). The "
        "approval names the file the check found, and only that file is restored: "
        "expected_sha256, when given, must be it.",
        {
            "grant_id": _GRANT,
            "expected_sha256": string(
                "The SHA-256 the check reported.", max_length=64
            ),
        },
        ("instance_id", "grant_id"),
    ),
    ToolSpec(
        "grant_restore_upload",
        "Open a one-use door to upload an archive for restore_from_upload: the "
        "server accepts your own ed25519 public key (ssh-keygen -t ed25519) for "
        "rsync into one directory only, write only, until it expires (12 hours) "
        "or a restore takes what arrived. Never give the private key. Answers the "
        "upload command, and the job that installs the key: the command works once "
        "that job has started.",
        _DEVELOPER,
        obj(
            {
                "instance_id": record_id("The instance the archive is for."),
                "public_key": string(
                    "Your public key, the line of the .pub file "
                    "(ssh-ed25519 AAAA…).",
                    max_length=1024,
                ),
            },
            required=("instance_id", "public_key"),
        ),
        handler="_tool_grant_restore_upload",
        confirm="always",
    ),
    ToolSpec(
        "revoke_restore_upload",
        "Close an upload door before it expires: its key stops being accepted "
        "and its directory, with whatever was sent through it, is removed from "
        "the server. For a door that is no longer needed, or whose private key "
        "may have leaked. A door a restore took is closed already.",
        _DEVELOPER,
        obj(
            {"instance_id": record_id("The instance."), "grant_id": _GRANT},
            required=("instance_id", "grant_id"),
        ),
        handler="_tool_revoke_restore_upload",
    ),
    ToolSpec(
        "check_restore_upload",
        "Check what arrived through an upload door: a job computes, on the "
        "server, the name, size and SHA-256 of the file sent; its log (get_job) "
        "ends with them. Then restore_from_upload restores that file.",
        _DEVELOPER,
        obj(
            {"instance_id": record_id("The instance."), "grant_id": _GRANT},
            required=("instance_id", "grant_id"),
        ),
        handler="_tool_check_restore_upload",
    ),
)


class McpCatalogRestore(models.AbstractModel):
    """The restore tools. They call model methods, not routes: none to classify."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the restore tools."""
        return super()._tool_specs() + list(TOOLS)


class McpToolsRestore(models.AbstractModel):
    """What each restore asks the user, and the instance's method it calls."""

    _inherit = "incubacloud.mcp.tools"

    # ── Shared ────────────────────────────────────────────────────────────

    @api.model
    def _restore_options(self, args):
        """Return *args* with every option said, as the call is bound to them.

        :param dict args: the validated arguments, ``instance_id`` among them
        :raises AccessError: for an instance the user may not read
        """
        instance = self._instance(args["instance_id"])
        return {
            **args,
            "neutralize": args.get(
                "neutralize", instance.environment != "production"
            ),
            "reset_base_url": args.get("reset_base_url", True),
            "backup_before_restore": bool(args.get("backup_before_restore")),
        }

    @api.model
    def _restore_message(self, args, source):
        """Return the approval of a restore of *args* from *source*.

        The restore's own refusals come first: the role, the instance as
        the user reads it, a host, and the destination a safety backup
        needs — a restore that would be refused is refused before anyone
        is asked.

        :param dict args: the prepared arguments
        :param str source: where the archive comes from, already quoted
        :raises AccessError: below the developer role, or for an instance
            the user may not read
        :raises UserError: without a host, or a safety backup without a
            destination
        """
        self.env["cloud.security.mixin"]._check_can_manage_backups()
        instance = self._instance(args["instance_id"])
        if not instance.host_id:
            raise UserError(self.env._("Instance has no host assigned."))
        backup_first = instance.environment == "production" or bool(
            args.get("backup_before_restore")
        )
        if backup_first and not instance.instance_backup_dst:
            raise UserError(
                self.env._(
                    "This instance has no resolvable backup backend, so a "
                    "pre-restore safety backup cannot be taken. Configure a "
                    "backup backend (instance, project or global) first."
                )
            )
        if instance.environment == "production":
            target = self.env._(
                "the production instance %(name)s (project %(project)s)",
                name=quoted(instance.name),
                project=quoted(instance.project_id.name or ""),
            )
        else:
            target = self.env._(
                "the instance %(name)s (project %(project)s)",
                name=quoted(instance.name),
                project=quoted(instance.project_id.name or ""),
            )
        how = [
            self.env._("neutralized")
            if args.get("neutralize")
            else self.env._("not neutralized"),
            self.env._("given the instance's own address")
            if args.get("reset_base_url")
            else self.env._("keeping the address it has"),
        ]
        if backup_first:
            how.append(self.env._("after a safety backup"))
        else:
            how.append(self.env._("without a safety backup"))
        return self.env._(
            "Replace the database and files of %(target)s with %(origin)s "
            "(%(how)s). Everything the instance holds now is lost.",
            target=target,
            origin=source,
            how=", ".join(how),
        )

    @api.model
    def _restore_payload(self, args, **payload):
        """Return the payload of ``restore_db`` for *args*: the options only.

        Built here, key by key: nothing else of the agent's reaches the
        executor's payload.
        """
        return payload | {
            "backup_before_restore": bool(args.get("backup_before_restore")),
            "neutralize": bool(args.get("neutralize")),
            "reset_base_url": bool(args.get("reset_base_url")),
        }

    # ── From a link ───────────────────────────────────────────────────────

    @api.model
    def _prepare_restore_from_link(self, args):
        """Refuse a link an agent may not give, and say every option.

        :raises UserError: for a link that is not ``https``, or that
            carries a user or a password — neither is quoted back
        """
        url = args["url"].strip()
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise UserError(
                self.env._(
                    "An agent gives an https link only: sftp and ftp links are "
                    "given in the panel (Import, From a link)."
                )
            )
        if "@" in parts.netloc:
            raise UserError(
                self.env._(
                    "A link with a user or a password is given in the panel "
                    "(Import, From a link): credentials never pass through an "
                    "agent. Use a presigned or share link instead."
                )
            )
        return self._restore_options({**args, "url": url})

    @api.model
    def _confirm_restore_from_link(self, args):
        """Name the instance, every piece of the link, cut, and the options.

        The link is checked as ``restore_from_url`` checks an agent's — a
        public address, a plain name, port 443, no server of the
        panel's — before anyone is asked. It is shown as ``told_link``
        shows it: a look-alike link of the same host does not read the
        same.
        """
        self._instance(args["instance_id"])._check_restore_link(
            args["url"], from_agent=True,
        )
        return self._restore_message(
            args,
            self.env._(
                "the archive its server downloads from %s",
                quoted(told_link(args["url"])),
            ),
        )

    @api.model
    def _record_restore_from_link(self, args):
        """Keep the link's host and file only, cut (``shown_link``)."""
        if not isinstance(args.get("url"), str):
            return args
        return {**args, "url": shown_link(args["url"])}

    @api.model
    def _show_restore_from_link(self, args):
        """Show every piece of the link, cut, as the approval does (``told_link``)."""
        if not isinstance(args.get("url"), str):
            return args
        return {**args, "url": told_link(args["url"])}

    @api.model
    def _tool_restore_from_link(self, args):
        """Restore as the dialog's From a link does: ``restore_from_url``."""
        job_id = self._instance(args["instance_id"]).restore_from_url(
            args["url"],
            backup_before_restore=bool(args.get("backup_before_restore")),
            neutralize=bool(args.get("neutralize")),
            reset_base_url=bool(args.get("reset_base_url")),
            from_agent=True,
        )
        return self._with_job_state(
            {"ok": True, "job_id": job_id, "link": shown_link(args["url"])}
        )

    # ── From a file on the server ─────────────────────────────────────────

    @api.model
    def _prepare_restore_from_host_file(self, args):
        """Say every option."""
        return self._restore_options(args)

    @api.model
    def _confirm_restore_from_host_file(self, args):
        """Name the instance, its server and the one path read."""
        instance = self._instance(args["instance_id"])
        return self._restore_message(
            args,
            self.env._(
                "the archive at %(path)s on the server %(host)s",
                path=quoted(rsync_path(instance.id)),
                host=quoted(instance.host_id.name or ""),
            ),
        )

    @api.model
    def _tool_restore_from_host_file(self, args):
        """Restore the file at the agreed path: ``restore_db`` in ``rsync``."""
        instance = self._instance(args["instance_id"])
        job_id = instance.restore_db(self._restore_payload(args, mode="rsync"))
        return self._with_job_state(
            {"ok": True, "job_id": job_id, "path": rsync_path(instance.id)}
        )

    # ── Through an upload door ────────────────────────────────────────────

    @api.model
    def _grant(self, args):
        """Return the upload door of *args*, as the user reads it.

        :raises UserError: for a door that is not this instance's
        """
        instance = self._instance(args["instance_id"])
        grant = self._record(
            "cloud.restore.upload.grant",
            args["grant_id"],
            self.env._("That upload grant does not exist."),
        )
        if grant.instance_id != instance:
            raise UserError(self.env._("That upload grant does not exist."))
        return grant

    @api.model
    def _prepare_grant_restore_upload(self, args):
        """Take the public key alone, in the one shape a grant installs.

        Whatever else the agent sent with it — a comment, or a private
        key by mistake — is neither audited nor quoted back.
        """
        return {
            **args,
            "public_key": self.env["cloud.restore.upload.grant"]._parse_public_key(
                args["public_key"]
            ),
        }

    @api.model
    def _confirm_grant_restore_upload(self, args):
        """Name the server, the instance, the key and how long it lasts."""
        self.env["cloud.security.mixin"]._check_can_manage_backups()
        instance = self._instance(args["instance_id"])
        if not instance.host_id:
            raise UserError(self.env._("Instance has no host assigned."))
        Grant = self.env["cloud.restore.upload.grant"]
        return self.env._(
            "Let the key %(key)s upload an archive for the instance %(name)s to "
            "the server %(host)s: into one directory, write only, for "
            "%(hours)s hours or until a restore takes it.",
            key=quoted(Grant._fingerprint(args["public_key"])),
            name=quoted(instance.name),
            host=quoted(instance.host_id.name or ""),
            hours=GRANT_HOURS,
        )

    @api.model
    def _tool_grant_restore_upload(self, args):
        """Open the door for the agent's key: ``grant_restore_upload``.

        The answer is what sending needs, and the job installing the key:
        no private key, which the panel never made, and not the
        directory, which the door's rsync names by itself.
        """
        instance = self._instance(args["instance_id"])
        grant = instance.grant_restore_upload(public_key=args["public_key"])
        port = grant["port"]
        port_flag = f" -p {port}" if port and port != 22 else ""
        return self._with_job_state(
            {
                "ok": True,
                "grant_id": grant["grant_id"],
                "job_id": grant["job_id"],
                "fingerprint": grant["fingerprint"],
                "expires_at": grant["expires_at"],
                "user": grant["user"],
                "host": grant["host"],
                "port": port,
                "host_key_fingerprint": instance.host_id.known_hosts_fingerprint
                or "",
                "upload_command": (
                    f'rsync -P --inplace -e "ssh{port_flag} -i <your private key '
                    f'file>" <your-backup.zip> {grant["user"]}@{grant["host"]}:'
                    "restore.zip"
                ),
            }
        )

    @api.model
    def _tool_revoke_restore_upload(self, args):
        """Close the door: ``revoke_restore_upload``, as the panel does.

        Asks nobody: closing a door only takes access away, and one whose
        key may have leaked must not wait for an approval. What was sent
        through it goes with it, and is sent again through a new door.
        """
        grant = self._grant(args)
        jobs = grant.instance_id.revoke_restore_upload(grant.id)
        if not jobs:
            return {
                "ok": True,
                "closed": False,
                "message": self.env._(
                    "That door is closed already: nothing to do."
                ),
            }
        return self._with_job_state({"ok": True, "closed": True, "job_id": jobs[0]})

    @api.model
    def _tool_check_restore_upload(self, args):
        """Ask the server what arrived: ``verify_restore_upload``."""
        grant = self._grant(args)
        job_id = grant.instance_id.verify_restore_upload(grant.id)
        return self._with_job_state({"ok": True, "job_id": job_id})

    @api.model
    def _prepare_restore_from_upload(self, args):
        """Bind the call to the file the last check found, and every option.

        The approval names that file; another check since — whoever
        holds the key may send another — changes the arguments, and the
        approval no longer holds.

        :raises UserError: before any check found a file, or for a
            ``expected_sha256`` that is not one
        """
        grant = self._grant(args)
        expected = (args.get("expected_sha256") or "").strip().lower()
        if expected and not _SHA256.fullmatch(expected):
            raise UserError(self.env._("expected_sha256 is not a SHA-256."))
        if not grant.received_sha256:
            raise UserError(
                self.env._(
                    "Nothing that can be restored has been found through this "
                    "door yet: send the archive as restore.zip, then call "
                    "check_restore_upload and wait for its job."
                )
            )
        return self._restore_options(
            {**args, "expected_sha256": expected or grant.received_sha256}
        )

    @api.model
    def _confirm_restore_from_upload(self, args):
        """Name the instance, the file the check found and the options."""
        grant = self._grant(args)
        if grant.received_sha256 != args["expected_sha256"]:
            raise UserError(
                self.env._(
                    "What arrived has changed since it was shown to you, or "
                    "the restore did not say which file it means. Check what "
                    "arrived again, then restore it."
                )
            )
        return self._restore_message(
            args,
            self.env._(
                "the file %(file)s uploaded through the door (%(bytes)s bytes, "
                "SHA-256 %(sha)s)",
                file=quoted(grant.received_filename or ""),
                bytes=grant.received_bytes,
                sha=grant.received_sha256,
            ),
        )

    @api.model
    def _tool_restore_from_upload(self, args):
        """Restore what the door received: ``restore_db`` in ``ssh_upload``."""
        grant = self._grant(args)
        job_id = grant.instance_id.restore_db(
            self._restore_payload(
                args,
                mode="ssh_upload",
                grant_id=grant.id,
                expected_sha256=args["expected_sha256"],
            )
        )
        return self._with_job_state({"ok": True, "job_id": job_id})
