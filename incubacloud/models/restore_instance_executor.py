import os
import posixpath
import shlex
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from ..net.outbound import OutboundError
from ..net.restore_source import (
    ALLOWED_SCHEMES,
    curl_resolve_argument,
    curl_url,
)
from ..net.restore_source import validate as validate_restore_url
from ..restore_space import (
    free_space_command,
    in_gib,
    largest_download,
    no_room_message,
    parse_free,
    room_needed,
)
from ..restore_staging import is_staged_upload
from ._odoo_versions import script_env
from .abstract_executor import (
    COMPOSE_OP_EXIT_UNCHECKED,
    AbstractSSHExecutor,
    JobRefused,
    handoff_archive_path,
    handoff_dir,
    handoff_sweep,
    same_chain,
    sql_escape_literal,
)
from .cloud_restore_upload_grant import SAFE_UPLOAD_NAME
from .move_steps_executor import parse_fingerprint
from .password_utils import merge_secret_key_lines
from .secret_key_escrow import SecretKeyEscrowMixin


def _curl_quoted(value):
    """Return *value* as a quoted string of a curl config file.

    ``\\``, ``"`` and line breaks escaped: curl has read a config file
    that way in every release a host may carry.

    :param str value: the option's value
    :rtype: str
    """
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _curl_config(login, password):
    """Return the curl config file that logs a download in.

    One ``user`` line, its value a quoted string (``_curl_quoted``). A
    netrc file would not do: quoted tokens came with curl 7.84, and
    Ubuntu 22.04 ships 7.81, which read the quotes as part of the
    password (28-oct-2026). curl splits the value at its first colon, so
    the login must not hold one (``restore_from_url`` refuses it).

    :param str login: the user name
    :param str password: the password, possibly empty
    :rtype: str
    """
    return f"user = {_curl_quoted(f'{login}:{password}')}\n"


def _curl_url_config(url):
    """Return the curl config line that names the link to download.

    In the config file, never in the command line: a presigned link's
    signature, or a share link's token, downloads the archive by itself,
    and ``ps`` shows a command line to every account of the host.

    :param str url: the link, as ``curl_url`` rebuilt it
    :rtype: str
    """
    return f"url = {_curl_quoted(url)}\n"


#: Exit statuses of a download that hit its size limit: curl's own
#: (``--max-filesize``, 63), and what a write past the shell's file size
#: limit ends with (curl's write error, 23).
_DOWNLOAD_TOO_LARGE = (63, 23)


def rsync_path(instance_id):
    """Return where an ``rsync`` restore of an instance takes its archive.

    One path per instance, fixed before any job exists, and never one the
    caller names: whoever asks for the restore copies the archive there
    as the host's SSH account, and the restore reads no other file.

    :param int instance_id: the instance restored
    :rtype: str
    """
    return f"/tmp/incubacloud-restore-{int(instance_id)}.zip"


def _where_its_name_says(path):
    """Return a shell test that *path* exists and resolves to itself.

    No link on the way to it, nor at its end: what is moved is the file
    its name says, inside the directory its name says.

    :param str path: an absolute path without a trailing slash
    :rtype: str
    """
    quoted = shlex.quote(path)
    return f'[ "$(realpath -e -- {quoted})" = {quoted} ]'


def _take_upload(granted, received, local):
    """Return the shell that takes an uploaded archive as a copy of its own.

    The file is first moved out of the grant's directory, under
    *received*: the name is then this job's, and no session of the key
    can put another file in its place. Only a regular file is read — a
    pipe would hold the copy forever. Then it is copied to *local*, a
    file no other name leads to, and the moved one removed. The key
    stays open until its revocation runs, after the restore, and a hard
    link sent beside the archive still names the moved file: whatever
    is written through it, in place, never reaches the copy, whose
    digest is the one checked (``_check_upload_digest``). A copy that
    fails leaves the moved file at *local*, to be given back.

    :param str granted: the archive in the grant's directory
    :param str received: where it is moved, in the job's directory
    :param str local: the copy restored, in the job's directory
    :rtype: str
    """
    src, moved, dst = (shlex.quote(p) for p in (granted, received, local))
    return (
        f"{_where_its_name_says(granted)}"
        f" && mv -fT -- {src} {moved}"
        f" && test -f {moved} && ! test -L {moved}"
        f" && {{ cp --reflink=auto -- {moved} {dst}"
        f" || {{ rm -f -- {dst}; mv -fT -- {moved} {dst}; false; }}; }}"
        f" && rm -f -- {moved}"
    )


class RestoreInstanceExecutor(SecretKeyEscrowMixin, AbstractSSHExecutor):
    """Restore a doodba instance database from a .zip backup.

    Payload:
        mode: 'browser' | 'from_job' | 'from_host' | 'rsync' — where the
            zip comes from. ``from_host`` consumes the archive a chained
            ``backup_download`` with ``handoff='host'`` handed over on
            its host, in a directory of its own (``handoff_dir``): same
            host → the archive is restored in place and the directory
            removed, nothing crosses the wire; different host → it is
            streamed source host → core disk → this host, never through
            RAM or an ``ir.attachment``, and the directory removed on the
            source once the restore succeeded. A failed restore leaves it
            for the next attempt; a restore that will not run any more —
            cancelled while it waited, or left behind by its chain — has
            a hidden job of the download's host remove it
            (``discard_handoff``); one a day old is swept, daily and when
            a handoff or a restore from one starts. ``from_job`` (the
            attachment relay) stays for operator-facing flows and old
            queued jobs.
            ``rsync`` takes ``/tmp/incubacloud-restore-<instance id>.zip``:
            a path agreed before the job exists, where whoever runs the
            restore copied the archive themselves as the host's SSH
            account. Any account could have created that path first, so
            only a regular file of this account's is taken from there,
            into the job's own directory, as ``ssh_upload`` takes its
            archive; a restore that does not get to its end gives it
            back.
        local_path / source_job_id: source, per mode
        neutralize: run ``click-odoo-restoredb --neutralize`` so the
            restored copy comes up with scheduled actions and outgoing
            mail disabled and the test banner on. Callers that copy a
            production database into another instance (clone to staging,
            refresh from production) set it; a host move must not, since
            that is the same production instance changing machines. On a
            staging, the placeholder server the neutralization inserts is
            then removed, so mail falls back to the stack's MailHog
            instead of failing; the real servers stay archived.
        reset_base_url: overwrite the ``web.base.url`` that travelled
            inside the dump with this instance's own domain, and drop
            ``web.base.url.freeze``. Skipped when the instance has no
            domain — writing an empty parameter would be worse than
            leaving the inherited one.
        verify_job: the ``move_fingerprint`` of the move this restore
            belongs to. Between the restore of the database and the
            start of Odoo, the same tables and the filestore are counted
            here and compared with what it counted on the source; any
            difference fails the restore before Odoo starts, and the
            move's cutover never runs.
        share_secret_key: give this instance the encryption keys of the
            instance the data comes from (a refresh from production): the
            restored database was encrypted with them. Odoo is then
            recreated rather than started, so it reads the new file.
        as_move: the archive is this same database changing hosts (a
            move), not a copy of it: it is loaded with ``--move``, so the
            database keeps its ``database.secret`` (the links it signed
            stay valid), ``database.uuid`` and ``web.base.url``. A copy has
            Odoo regenerate the three, the base URL as
            ``http://localhost:8069``. Sessions are not carried: they live
            in the data dir's ``sessions/``, which the archive does not
            hold, so users sign in again on the new host. Only
            ``move_to_host`` sets it, and it is honoured only while the
            instance is moving to this job's host (``_loads_as_move``):
            a restore anyone else queues loads a copy.
    """

    _job_type = "restore_instance"

    #: Set by ``before_execute`` when the instance takes the keys of the
    #: one whose data it receives; ``get_commands`` then writes them.
    _writes_secret_key = False

    #: ``(url, pin)`` of a ``from_url`` restore, once checked here
    #: (``_checked_link``).
    _link = None

    #: The largest archive a ``from_url`` restore may download, from what
    #: ``before_execute`` measured free on the host (``restore_space``).
    _download_limit = None

    def _inst(self):
        return self.job.instance_id

    def _loads_as_move(self, inst, payload):
        """Return whether this restore loads its archive as moved.

        ``as_move`` keeps the archive's ``database.uuid`` and
        ``database.secret``. Asked by anyone but a move — a restore
        queued straight through ``cloud.job.enqueue`` with it in the
        payload — a staging could take production's: two databases
        claiming to be one, and signed links valid in both. So it counts
        only while *inst* is moving to the host this job runs on, which
        ``move_to_host`` marks before it queues the chain and nobody
        else may write.

        :param inst: the instance restored.
        :param dict payload: this job's payload.
        :rtype: bool
        """
        if not payload.get('as_move'):
            return False
        target = inst.move_target_host_id
        return bool(target) and target.id == self.job.host_id.id

    def _verify_fingerprint(self):
        """Return the source's fingerprint this restore checks against.

        Only that of a step of its own chain (``same_chain``): the ID in
        the payload is the caller's to write.

        :return: the fingerprint (``parse_fingerprint``'s shape), or None
            when the payload names none, or one that cannot be used
        :rtype: dict | None
        """
        verify_id = (self.job.payload or {}).get('verify_job')
        if not str(verify_id or '').isdigit():
            return None
        source = self.job.sudo().browse(int(verify_id)).exists()
        if not source or not same_chain(self.job, source):
            return None
        result = source.result or {}
        if not isinstance(result.get('tables'), dict) \
                or not isinstance(result.get('files'), int):
            return None
        return result

    def pre_run_checks(self):
        """Refuse a restore that must verify and has nothing to verify with.

        Before connecting: the database is not touched.

        :raises JobRefused: when the payload names a ``verify_job`` whose
            fingerprint is missing
        """
        if (self.job.payload or {}).get('verify_job') \
                and self._verify_fingerprint() is None:
            raise JobRefused(
                "Refused: the fingerprint of the source this restore checks "
                "against is missing, so the copy could not be verified."
            )
        super().pre_run_checks()

    def _verify_step(self, d, inst, dbname):
        """Return the integrity check against the source, or nothing.

        :param str d: the instance's directory
        :param inst: the ``cloud.instance``
        :param str dbname: its database
        :return: a tuple of zero or one command
        """
        fingerprint = self._verify_fingerprint()
        if fingerprint is None:
            return ()
        tables = fingerprint['tables']
        return ((
            "Verify integrity",
            self.run_script("move_steps.sh", [
                "verify", d, inst.postgres_username or 'odoo', dbname,
                str(fingerprint['files']),
                *(f"{name}={count}" for name, count in sorted(tables.items())),
            ]),
            {"stop_on_failure": True},
        ),)

    def _source_job(self):
        """Return the chained job referenced by ``source_job_id``, or an
        empty recordset when the payload carries none.

        Read in the job's environment: a job asked for without superuser
        rights runs without them (``cloud.job._dispatch_single``), and
        takes its data only from a job its creator may read — the record
        rules of ``cloud.job`` decide, as they do in the panel. ``exists``
        alone would not: it ignores them.

        :raises AccessError: when the source is a job the creator cannot
            read
        """
        source_job_id = (self.job.payload or {}).get('source_job_id')
        if not source_job_id:
            return self.job.browse()
        source = self.job.browse(int(source_job_id)).exists()
        source.check_access('read')
        return source

    def _remote_path(self):
        """Path of the archive on this job's host.

        ``from_host`` on the producer's own host restores the handoff
        archive in place (the trailing cleanup step then removes it).
        Every other archive — uploaded, relayed or downloaded by this
        job, or taken from where an ``ssh_upload`` key or an ``rsync``
        copy put it — is in the job's own directory under a name of its
        own, where no other account can read it or put one of its own
        first, and its uploader can no longer change it. Computed from
        the payload alone so it needs no ordering with
        ``before_execute``.
        """
        payload = self.job.payload or {}
        if payload.get('mode') == 'from_host':
            source = self._source_job()
            if source and source.host_id == self.job.host_id:
                return handoff_archive_path(self.env, source.id)
        return self._job_path("restore.zip")

    def _rsync_path(self):
        """Return where an ``rsync`` restore expects its archive copied.

        Agreed before the job exists, so in /tmp under a name anybody
        can tell: read only to take a regular file of this account's
        from it (``before_execute``) and to give it back
        (``_discard_staging``).

        :rtype: str
        """
        return rsync_path(self._inst().id)

    def _curl_config_path(self):
        """Return where a ``from_url`` restore stages its credential.

        :rtype: str
        """
        return self._job_path("download.curlrc")

    def _granted_path(self):
        """Return the file the ``ssh_upload`` key wrote, or None.

        Inside the grant's own directory: that is the only place its key
        could have written, so reading anywhere else would mean reading
        something it did not put there. Its name is the uploader's: only
        one safe component (``SAFE_UPLOAD_NAME``) is ever used — anything
        else, a ``..`` or a name carrying a line of the listing, names
        no file — and it is only ever passed quoted, to take the file
        into the job's directory (``before_execute``) or give it back
        (``_discard_staging``).

        :return: the path, or None when the mode is another, the grant
            is gone or its name is not one the restore may use
        :rtype: str | None
        """
        if (self.job.payload or {}).get('mode') != 'ssh_upload':
            return None
        grant = self._grant()
        if not grant:
            return None
        name = grant.received_filename or 'restore.zip'
        if not SAFE_UPLOAD_NAME.fullmatch(name):
            return None
        path = grant._remote_path(name)
        if posixpath.dirname(posixpath.normpath(path)) != grant._directory():
            return None
        return path

    async def _discard_staging(self, transport):
        """Give an uploaded archive back before the job's directory goes.

        A restore that did not get to its end leaves the archive where the
        key or the copy put it, so the next attempt needs no second
        upload; one that did removed it already. Then the job's directory
        goes as usual. Best effort, never raising: it runs on the way out
        of every run.

        Made this account's again first: ``verify-file`` handed it to the
        container's user, and this account could neither read it to
        check it again nor, for ``rsync``, take it next time. In the
        job's own directory nobody else can swap it for a link before
        that ``sudo``. ``ssh_upload``: back into the grant's directory,
        if that is still where its name says. ``rsync``: back to the
        agreed path.
        """
        mode = (self.job.payload or {}).get('mode')
        if self._job_dir_created and mode in ('ssh_upload', 'rsync'):
            with suppress(Exception):
                await transport.run(self._give_back_command(mode))
        await super()._discard_staging(transport)

    def _give_back_command(self, mode):
        """Return the command that puts an uploaded archive back, or ``true``.

        :param str mode: ``ssh_upload`` or ``rsync``
        :rtype: str
        """
        local = shlex.quote(self._job_path("restore.zip"))
        granted = self._granted_path()
        if granted:
            directory = posixpath.dirname(granted)
            back = (
                f"{_where_its_name_says(directory)}"
                f" && mv -fT -- {local} {shlex.quote(granted)}"
            )
        elif mode == 'rsync':
            back = f"mv -fT -- {local} {shlex.quote(self._rsync_path())}"
        else:
            return "true"
        return (
            f"test -f {local} && ! test -L {local}"
            f" && {{ test -O {local}"
            f' || sudo -n chown "$(id -u):$(id -g)" -- {local}; }}'
            f" && {back}"
        )

    def _local_handoff_dir(self):
        """Return the handoff directory this job restores in place, or None.

        Only for ``from_host`` on the producer's own host.

        :rtype: str | None
        """
        payload = self.job.payload or {}
        if payload.get('mode') != 'from_host':
            return None
        source = self._source_job()
        if source and source.host_id == self.job.host_id:
            return handoff_dir(self.env, source.id)
        return None

    def on_cancelled_queued(self):
        """A restore cancelled in the queue takes its handoff with it.

        The download that produced it ran; this restore never will, and
        the archive — a production database — would otherwise wait for
        the day-old sweep. Queued as a hidden job of the download's host
        (``discard_handoff``), never removed from here: this runs in the
        transaction that cancels, a dispatcher's with the queues' locks
        held among them, and a host that is slow or down must not hold
        those. A host that cannot be reached keeps it for the sweep.
        """
        source = self._handoff_to_discard()
        if source:
            self.env['cloud.job'].sudo().enqueue(
                source.host_id.id, False, 'discard_handoff',
                payload={'source_job_id': source.id},
            )

    def on_never_run(self):
        """A restore its chain left behind takes its handoff with it, as
        one cancelled in the queue does (``on_cancelled_queued``)."""
        self.on_cancelled_queued()

    def _handoff_to_discard(self):
        """Return the download whose handoff this restore leaves behind.

        Only for ``from_host``, only when the download ran — a chain
        cancelled whole handed nothing over — and only a download of
        this restore's own chain. The hooks that ask run as the
        superuser, so reading the download proves nothing: anyone who
        may queue a restore could otherwise name another chain's
        download, cancel the restore, and have that chain's archive
        removed under its restore (28-oct-2026).

        :return: the ``backup_download`` job, with ``sudo``, or an empty
            recordset
        """
        payload = self.job.payload or {}
        # A chain never dispatched still names its steps by position.
        if payload.get('mode') != 'from_host' \
                or not str(payload.get('source_job_id') or '').isdigit():
            return self.job.browse()
        source = self._source_job().sudo()
        if not source.host_id or not source.queue_job_id.date_started \
                or not same_chain(self.job, source):
            return self.job.browse()
        return source


    def _curl_config_content(self):
        """Return the config a ``from_url`` restore downloads with.

        The link (``_curl_url_config``) and, when it logs in, its
        credential (``_curl_config``): both secrets, staged owner-only.

        :rtype: str
        :raise ValueError: when the link is not one a host may fetch
        """
        url, _resolve = self._checked_link()
        config = _curl_url_config(url)
        credential = self.job._restore_url_credential()
        if credential:
            _machine, user, password = credential
            config += _curl_config(user, password)
        return config

    def _checked_link(self):
        """Return the link to download and the address curl must dial.

        Checked again here, whoever queued the job: the payload is what
        its caller wrote, and only ``restore_from_url`` checks a link
        before queuing it. The pin is computed here too; the payload's
        is never used. Checked once per run, so every step uses the
        same answer. The link returned is rebuilt from the name checked
        (``curl_url``): its host is the pin's, byte for byte, so curl
        never looks the name up itself. An agent's link
        (``link_policy``) is held to its rules again here: port 443, and
        no server of this panel's — a name may resolve to one by now.
        The link is read where ``restore_from_url`` keeps it, encrypted
        (``cloud.job._restore_url_link``).

        :return: ``(url, resolve)``, the latter in ``--resolve``'s shape
        :rtype: tuple[str, str]
        :raise ValueError: when the link is not one a host may fetch
        """
        if self._link is None:
            url = self.job._restore_url_link()
            options = {}
            if (self.job.payload or {}).get('link_policy') == 'agent':
                options = {
                    'only_port': 443,
                    'refused': self.env['cloud.host']._panel_own_networks(),
                }
            try:
                if options and urlsplit(url).scheme != 'https':
                    raise OutboundError("an agent gives an https link only")
                parts, address, port = validate_restore_url(url, **options)
            except OutboundError as exc:
                raise ValueError(
                    f"The link cannot be used: {exc}. Nothing was "
                    "downloaded."
                ) from exc
            self._link = (
                curl_url(parts), curl_resolve_argument(parts, address, port),
            )
        return self._link

    def _download_command(self, remote):
        """Return the step that fetches the archive onto the host.

        Deliberate choices. curl reads no configuration of the host's:
        ``-q`` (first, or it is ignored) skips the account's ``.curlrc``,
        and ``--noproxy '*'`` any proxy its environment names — either
        could send the download elsewhere than the pinned address. No
        ``-C -``: each attempt downloads into a job directory of its own,
        removed when it fails, so there is never a partial file to
        resume. Redirects are *not* followed:
        the address was validated and pinned here, and a redirect is
        precisely how that validation would be escaped. And the
        link and its credentials, if any, are in a curl config file
        ``before_execute`` staged owner-only in the job's directory,
        removed when the download ends — a password, or a link that
        downloads by itself, in the command line would be readable in
        ``ps`` by anyone on the host. Sent only to the address the link
        names: no redirect is followed. Every value is one quoted word:
        the address is the requester's, and a quote in it ended the
        quoting it was written in (28-oct-2026).

        And it writes no more than ``_download_limit``, what the disk
        free leaves (``restore_space``): curl refuses a larger file it
        is told the size of (``--max-filesize``), and a file size limit
        of the shell (``ulimit -f``, in KiB in bash) stops a server that
        does not say it, or never stops sending, which ``--max-filesize``
        lets through before curl 8.4 (Ubuntu 22.04 ships 7.81). Past it
        the write fails rather than the signal killing curl
        (``trap '' XFSZ``).

        :param remote: path the archive must land on.
        :return: an executor command tuple.
        """
        _url, resolve = self._checked_link()
        if self._download_limit is None:
            raise ValueError(
                "The free disk on the server was not measured: nothing was "
                "downloaded."
            )
        limit = self._download_limit
        # Only the schemes a link may name, and no redirect but to https
        # should one ever be followed. No globbing: the address is one.
        # An ftp server's PASV answer names the address of its data
        # connection, which before curl 7.74 was dialled as given: any
        # address, past the pin. ``--ftp-skip-pasv-ip`` keeps the data
        # connection on the pinned one (other schemes ignore it).
        config = self._curl_config_path()
        args = [
            "bash", "-c", 'trap "" XFSZ; ulimit -f "$1" && shift && exec "$@"',
            "download", str(limit // 1024 + 1),
            "curl", "-q", "-fsS", "--max-time", "86400", "--globoff",
            "--noproxy", "*", "--ftp-skip-pasv-ip",
            "--proto", "=" + ",".join(ALLOWED_SCHEMES),
            "--proto-redir", "=https",
            "--resolve", resolve,
            "--max-filesize", str(limit),
            "--config", config,
            "-o", remote,
        ]
        command = (
            f"{shlex.join(args)}; rc=$?; rm -f {shlex.quote(config)}; exit $rc"
        )
        return (
            "Download the archive",
            command,
            {"stop_on_failure": True},
        )

    def _grant(self):
        """Return the upload grant this job consumes, if it names one."""
        grant_id = (self.job.payload or {}).get('grant_id')
        if not grant_id:
            return self.env['cloud.restore.upload.grant'].browse()
        return self.env['cloud.restore.upload.grant'].browse(
            int(grant_id),
        ).exists()

    async def _free_bytes(self, transport):
        """Return the bytes free on the disk of this job's directory.

        :param transport: the job's open transport, its directory made
        :rtype: int
        :raise RuntimeError: when the host does not say
        """
        answer = await transport.run(free_space_command(self._job_dir()))
        free = parse_free(answer.stdout) if answer.exit_status == 0 else None
        if free is None:
            raise RuntimeError(
                "The free disk on the server could not be measured: nothing "
                "was restored."
            )
        return free

    async def _check_room(self, transport, archive, copies):
        """Refuse an archive on the host the disk free cannot restore.

        :param transport: the job's open transport, its directory made
        :param str archive: the archive's path on the host
        :param int copies: further copies of it the restore makes
            (``restore_space.room_needed``)
        :raise RuntimeError: without room, or when its size or the space
            free cannot be read
        """
        sized = await transport.run(
            f"stat -c %s -- {shlex.quote(archive)}",
        )
        size = parse_free(sized.stdout) if sized.exit_status == 0 else None
        if size is None:
            raise RuntimeError(
                "The size of the archive on the server could not be read: "
                "not restored."
            )
        free = await self._free_bytes(transport)
        needed = room_needed(size, copies)
        if free < needed:
            raise RuntimeError(no_room_message(
                free, needed, f"an archive of {in_gib(size)}",
            ))

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        payload = self.job.payload or {}
        mode = payload.get('mode')

        if mode == 'browser':
            local_path = payload.get('local_path')
            if not local_path:
                raise ValueError(
                    "Backup file not found on Odoo server. "
                    "Please re-upload and try again."
                )
            # Defense-in-depth: ``local_path`` must name a file the
            # /cloud/instance/<id>/restore controller staged for *this*
            # instance. Anything else (e.g. ``/etc/odoo/odoo.conf`` passed
            # directly via JSON-RPC) is rejected — the executor would
            # otherwise both upload and unlink the target file.
            if not is_staged_upload(local_path, self._inst().id):
                raise ValueError(
                    "local_path must be an upload staged by the restore "
                    "controller for this instance (got %r)." % (local_path,)
                )
            resolved = str(Path(local_path).resolve())
            if not Path(resolved).exists():
                raise ValueError(
                    "Backup file not found on Odoo server. "
                    "Please re-upload and try again."
                )
            # ``finally``: the archive is up to 2 GiB and this is the only
            # code that deletes it. Leaving it behind on a failed transfer
            # (host down, disk full, connection dropped) would pin that
            # much disk until the container is recreated, and the operator
            # retries by uploading again anyway.
            try:
                await self._ensure_job_dir(transport)
                self._sys("Uploading backup to remote host via SFTP...")
                await transport.upload_file(resolved, self._remote_path())
                self._sys("✓ Backup transferred to remote host.")
            finally:
                with suppress(OSError):
                    Path(resolved).unlink()

        elif mode == 'from_job':
            source_job_id = payload.get('source_job_id')
            if not source_job_id:
                raise ValueError("Missing source_job_id in payload")
            # The attachment is read with ``sudo``: the job it hangs off
            # is read first, as the job's creator may (``_source_job``).
            self._source_job()
            att = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'cloud.job'),
                ('res_id', '=', int(source_job_id)),
            ], limit=1, order='id desc')
            # ``raw`` reads the filestore bytes directly; ``datas`` would
            # materialise a second, base64 copy of the archive in RAM.
            data = att.raw if att else b""
            if not data:
                raise ValueError(
                    "Backup attachment not found from source job"
                )
            self._sys("Retrieving backup from previous job…")
            fd, local_path = tempfile.mkstemp(
                prefix=".incubacloud-restore-", suffix=".zip",
            )
            os.close(fd)
            try:
                Path(local_path).write_bytes(data)
                await self._ensure_job_dir(transport)
                self._sys("Uploading backup to remote host via SFTP…")
                await transport.upload_file(local_path, self._remote_path())
                self._sys("✓ Backup transferred to remote host.")
            finally:
                with suppress(OSError):
                    Path(local_path).unlink()

        elif mode == 'from_host':
            source = self._source_job()
            if not source:
                raise ValueError(
                    "Missing or unknown source_job_id in payload"
                )
            if not source.host_id:
                raise ValueError("Source job has no host")
            # What chains that never reached their restore left on this
            # host: a day old, this account's, best effort.
            with suppress(Exception):
                await transport.run(handoff_sweep(self.env, self.job.host_id))
            src_path = handoff_archive_path(self.env, source.id)
            if source.host_id == self.job.host_id:
                # Producer and consumer share the host: the archive is
                # already where _remote_path() points. The "Verify backup
                # file" step confirms it exists.
                # Not its path: no other account should learn it.
                self._sys("Using the backup handed over on this host.")
                return
            # Cross-host: relay through the core's *disk* — both SFTP
            # legs stream file-to-file, so memory stays flat no matter
            # the archive size. The source copy is only removed in
            # on_success, which keeps a failed restore retryable.
            self._sys(
                f"Fetching backup from {source.host_id.name}…"
            )
            fd, local_path = tempfile.mkstemp(
                prefix=".incubacloud-handoff-", suffix=".zip",
            )
            os.close(fd)
            try:
                async with source.host_id.get_transport() as src_transport:
                    await src_transport.download_file(src_path, local_path)
                await self._ensure_job_dir(transport)
                self._sys("Uploading backup to this host via SFTP…")
                await transport.upload_file(local_path, self._remote_path())
            finally:
                with suppress(OSError):
                    Path(local_path).unlink()
            self._sys("✓ Backup transferred to this host.")

        elif mode == 'rsync':
            agreed = self._rsync_path()
            self._sys(f"Using the archive copied to {agreed}…")
            # Anybody can create a path in /tmp first: only a regular
            # file of this account's is taken, into the job's directory,
            # before ``verify-file`` hands it to ``sudo``.
            await self._ensure_job_dir(transport)
            local = self._remote_path()
            quoted = shlex.quote(agreed)
            taken = await transport.run(
                f"test -f {quoted} && ! test -L {quoted} && test -O {quoted}"
                f" && mv -fT -- {quoted} {shlex.quote(local)}"
            )
            if taken.exit_status != 0:
                raise RuntimeError(
                    f"{agreed} is not a regular file of the account this "
                    "panel connects as: not restored. Copy the archive "
                    "there as that account and try again."
                )
            await self._check_regular_file(transport, local)
            await self._check_room(transport, local, copies=0)

        elif mode == 'ssh_upload':
            grant = self._grant()
            if not grant:
                raise ValueError(
                    "The upload grant for this restore no longer exists."
                )
            grant._check_job(self.job)
            # The digest the operator confirmed, checked again here: a
            # check run since the restore was asked for recorded another
            # file, and nothing is taken.
            expected = payload.get('expected_sha256')
            if not expected or expected != grant.received_sha256:
                raise ValueError(
                    "The uploaded archive is not the one shown when this "
                    "restore was asked for: not restored. Check what "
                    "arrived again, then restore it."
                )
            granted = self._granted_path()
            if not granted:
                raise ValueError(
                    f"The uploaded file's name ({grant.received_filename!r}) "
                    "cannot be restored: only letters, digits, dots, dashes "
                    "and underscores, starting with a letter or a digit. "
                    "Send the archive again as restore.zip and check it."
                )
            self._sys(
                f"Using the archive uploaded through the temporary key "
                f"({grant.received_filename or 'restore.zip'})…"
            )
            # The key has done its job. Closing it here rather than at the
            # end means a restore that fails still leaves no door open —
            # the archive is already on the host, so a retry needs no
            # second upload. Both writes on one durable cursor: from the
            # job's own transaction a failure took them back, and even on
            # success the runner's final ``clear()`` drops whatever was
            # never flushed — measured, it dropped the revocation's link
            # to its queue job. Queued with ``sudo``, as every head of a
            # queue is: a restore asked for without superuser rights runs
            # without them (``_check_internal_queue_flags``).
            with self._durable_env() as env:
                grant.with_env(env)._mark_used()
                env['cloud.job'].sudo().enqueue(
                    grant.host_id.id, grant.instance_id.id,
                    'revoke_restore_upload_key',
                    payload={'grant_id': grant.id, 'keep_directory': True},
                    bypass_running_check=True,
                )
            # Copied into the job's own directory, under a name of its
            # own (``_take_upload``): a session the uploader still holds
            # can no longer change it, and the name it chose — passed
            # quoted here, nowhere else — never reaches a step. Only if
            # it is where its name says, in the grant's directory with no
            # link on the way. Then it must be a file: ``verify-file``
            # hands it to the odoo user with ``sudo``, which would follow
            # a link to whatever the uploader named. Not without room for
            # that copy and what follows (``restore_space``).
            await self._ensure_job_dir(transport)
            await self._check_room(transport, granted, copies=1)
            local = self._remote_path()
            taken = await transport.run(_take_upload(
                granted, self._job_path("upload.received"), local,
            ))
            if taken.exit_status != 0:
                raise RuntimeError(
                    "The uploaded archive could not be taken from the "
                    f"grant's directory (exit {taken.exit_status})."
                )
            await self._check_regular_file(transport, local)
            await self._check_upload_digest(transport, local, expected)

        elif mode == 'from_url':
            # Before the host is told anything.
            self._checked_link()
            # The download step writes it into the job's directory, and
            # reads the link and its credential, if any, from a file only
            # this account can read there. No larger than the disk free
            # leaves, measured there first.
            await self._ensure_job_dir(transport)
            free = await self._free_bytes(transport)
            self._download_limit = largest_download(free)
            if not self._download_limit:
                raise RuntimeError(no_room_message(
                    free, room_needed(0), "an archive from a link",
                ))
            await self._upload_private_files(
                transport, {self._curl_config_path(): self._curl_config_content()},
            )
            self._sys(
                "Downloading the archive on the host (at most "
                f"{in_gib(self._download_limit)}, with {in_gib(free)} free)…"
            )

        else:
            raise ValueError(f"Unknown restore mode: {mode!r}")

        if payload.get('share_secret_key'):
            await self._take_source_secret_key(transport)

    async def _check_upload_digest(self, transport, local, expected):
        """Refuse an archive other than the one whose digest was shown.

        The key stays open until its revocation runs, after this job:
        between the check the operator confirmed and the copy into the
        job's directory, the uploader could send another file under the
        same name, or write through another name of the same file. The
        copy is a file of its own, out of the key's reach, so its digest
        is the one that counts.

        :param transport: the job's open transport
        :param str local: the archive in the job's directory
        :param str expected: the SHA-256 the check recorded
        :raise RuntimeError: when it is not that one, or none was recorded
        """
        result = await transport.run(f"sha256sum -- {shlex.quote(local)}")
        found = (result.stdout or "").split()[:1]
        if result.exit_status != 0 or not expected or found != [expected]:
            raise RuntimeError(
                "The uploaded archive changed after it was checked, or was "
                "never checked: not restored. Check what arrived again, "
                "then restore it."
            )

    async def _take_source_secret_key(self, transport):
        """Give this instance the keys of the instance its data comes from.

        A staging refreshed from production receives a database encrypted
        with production's key; with its own, its Odoo could not read a
        secret of it. Production's chain comes first, then what the
        staging's host holds and the panel's copy, so nothing is lost.
        Production's chain is the panel's copy, or what its host holds
        when the panel has none yet.

        :param transport: the job's open transport (this instance's host)
        """
        source = self._source_job().instance_id
        if not source or source == self._inst():
            return
        theirs = source.sudo().secret_key or ""
        if not theirs and source.host_id:
            async with source.host_id.get_transport() as src_transport:
                theirs = await self._read_host_secret_key(
                    src_transport, source,
                )
        if not merge_secret_key_lines(theirs):
            self._sys(
                f"⚠ The encryption key of '{source.name}' is not known: "
                f"this instance keeps its own, and may not read the "
                f"secrets its new data holds."
            )
            return
        held = await self._read_host_secret_key(transport)
        chain = self._keep_secret_key(theirs, held)
        if chain == merge_secret_key_lines(held):
            return
        await self._stage_secret_key(transport, chain)
        self._writes_secret_key = True
        self._sys(
            f"This instance takes the encryption keys of '{source.name}', "
            f"whose data it receives."
        )

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        remote = self._remote_path()
        dbname = inst.postgres_dbname or 'prod'
        payload = self.job.payload or {}
        neutralize = "1" if payload.get('neutralize') else "0"
        load_as = "move" if self._loads_as_move(inst, payload) else "copy"

        cmds = []
        if payload.get('mode') == 'from_url':
            cmds.append(self._download_command(remote))
        cmds += [
            (
                "Verify backup file",
                self.run_script("restore.sh", ["verify-file", d, remote]),
                {"stop_on_failure": True},
            ),
            (
                "Stop Odoo service",
                self.run_script("compose_op.sh", [d, "stop", "odoo"]),
                {"stop_on_failure": True},
            ),
            (
                "Restore database",
                self.run_script(
                    "restore.sh",
                    ["restore-db", d, dbname, remote, neutralize, load_as],
                    env=script_env(inst.odoo_version),
                ),
                {"stop_on_failure": True},
            ),
            # Right after the restore, before anything writes to the
            # database (the base URL, the connector) and before Odoo
            # starts: what is compared is what the dump brought.
            *self._verify_step(d, inst, dbname),
        ]

        # Only where ``smtplocal`` is the mail catcher: a production
        # stack resolves the same name to the real relay.
        if neutralize == "1" and inst.environment != "production":
            cmds.append((
                "Route mail to the mail catcher",
                self.run_script(
                    "restore.sh",
                    [
                        "use-mail-catcher", d,
                        inst.postgres_username or "odoo",
                        dbname,
                    ],
                ),
            ))

        # Before "Ensure incubacloud_connect" on purpose: that step boots
        # an Odoo which reads web.base.url. ``db`` stays up throughout the
        # restore (only ``odoo`` is stopped), so psql over compose works.
        base_url = self._base_url() if payload.get('reset_base_url') else ""
        if base_url:
            cmds.append((
                "Reset base URL",
                self.run_script(
                    "restore.sh",
                    [
                        "set-base-url", d,
                        inst.postgres_username or "odoo",
                        dbname,
                        sql_escape_literal(base_url),
                        "http://localhost:8069",
                    ],
                ),
            ))

        # A container keeps the environment it was created with: Odoo is
        # recreated (``up``) when it has a new key to read.
        start_op = "start"
        if self._writes_secret_key:
            cmds.append(self._write_secret_key_step(d))
            start_op = "up"
        cmds += [
            (
                "Ensure incubacloud_connect",
                self.run_script(
                    "restore.sh",
                    ["ensure-connect", d, inst.postgres_username or "odoo", dbname],
                ),
            ),
            (
                "Start Odoo service",
                self.run_script("compose_op.sh", [d, start_op, "odoo"]),
                {"continue_on": (COMPOSE_OP_EXIT_UNCHECKED,)},
            ),
            # Left inline: a lone ``rm`` is not worth a versioned
            # script, and unquoted the remote shell expands any ``~``.
            # A handoff restored in place goes with its directory: the
            # restore made the archive the odoo user's, and only the
            # directory's owner can remove it.
            (
                "Remove remote backup file",
                f"rm -rf {self._local_handoff_dir()}"
                if self._local_handoff_dir() else f"rm -f {remote}",
            ),
        ]
        return cmds

    def parse_results(self, results):
        # Odoo is up on the restored database when only the check after
        # its start failed: a warning, not a failed restore.
        results = self._accept_unchecked_compose_op(results, "Start Odoo service")
        errors = []
        for label, data in results.items():
            if label in ("Remove remote backup file",
                         "Ensure incubacloud_connect"):
                continue
            if label == "Verify integrity" and data.get('exit_status', 1) != 0:
                # The script names each difference: that is the error.
                found = [
                    line.strip() for line in (data.get('stdout') or '').splitlines()
                    if line.startswith("Integrity check failed")
                ]
                errors.extend(found or [
                    f"'{label}' exited with status {data.get('exit_status')}"
                ])
                continue
            if label == "Download the archive" \
                    and data.get('exit_status') in _DOWNLOAD_TOO_LARGE:
                errors.append(
                    "The download stopped at the largest archive the free "
                    f"disk of the server takes ({in_gib(self._download_limit or 0)}"
                    "), or could not write on it (curl status "
                    f"{data.get('exit_status')}). Nothing was restored: free "
                    "space on the server, or restore a smaller archive."
                )
                continue
            if data.get('exit_status', 1) != 0:
                errors.append(
                    f"'{label}' exited with status"
                    f" {data.get('exit_status')}"
                )
        return errors

    async def on_success(self, results):
        self._sys("✓ Database restored successfully.")
        await self._cleanup_cross_host_handoff()
        inst = self._inst()
        inst.write({"status": "ok", **inst._running_confirmed_vals()})
        self._call_addon_hook(inst, "_after_restore_hook", "restore")
        if inst.pr_number:
            inst._post_or_update_pr_comment(inst._pr_preview_ready_body())

    async def _cleanup_cross_host_handoff(self):
        """Best-effort removal of the handoff on the *source* host after
        a successful cross-host restore.

        Same-host needs nothing here: ``_remote_path()`` is in the handoff
        itself and the trailing "Remove remote backup file" step already
        removed its directory. On failure the archive is left in place on
        purpose — a retried restore fetches it again instead of dying on
        a missing file; the day-old sweep (``handoff_sweep``) takes it if
        none comes.
        """
        payload = self.job.payload or {}
        if payload.get('mode') != 'from_host':
            return
        source = self._source_job()
        if not source or not source.host_id \
                or source.host_id == self.job.host_id:
            return
        with suppress(Exception):
            async with source.host_id.get_transport() as src_transport:
                await src_transport.run(
                    f"rm -rf {handoff_dir(self.env, source.id)}"
                )
            self._sys("✓ Staged backup removed from the source host.")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._inst().write({"status": "error"})


class DiscardHandoffExecutor(AbstractSSHExecutor):
    """Remove the archive a ``backup_download`` handed over, on its host.

    Queued by a ``from_host`` restore that will not run any more
    (``RestoreInstanceExecutor.on_cancelled_queued`` and
    ``on_never_run``): the transaction that says so never reaches a host
    itself. Hidden, so it waits for nothing on the host and nothing
    waits for it; a host that cannot be reached keeps the archive for
    the day-old sweep.

    Payload:
        source_job_id: the download whose handoff goes. Its directory is
            recomputed from it (``handoff_dir``), on the host only: it is
            neither stored nor logged.
    """

    _job_type = "discard_handoff"

    # A host away for a moment is tried again rather than given up on.
    _retry_on_connection_loss = True

    def get_commands(self):
        """Return the one step that removes the handoff, or nothing."""
        source_id = (self.job.payload or {}).get('source_job_id')
        if not str(source_id or '').isdigit():
            return []
        return [(
            "Remove the handed-over archive",
            f"rm -rf {handoff_dir(self.env, int(source_id))}",
        )]

    async def on_success(self, results):
        """Say it is done, without saying where it was."""
        self._sys("✓ The handed-over archive is gone.")
