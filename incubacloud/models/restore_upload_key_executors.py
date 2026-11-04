"""Install, verify and remove the one-use upload key on a host.

Three small jobs around ``cloud.restore.upload.grant``: one opens the
door, one says what came through it, one closes it. They are separate
because the middle one is the operator's decision point — nothing is
restored until they have seen the name, the size and the digest of what
actually landed, computed on the host rather than promised by the
browser that sent it.
"""
import logging
import posixpath
import re
import shlex

from ..restore_space import (
    RESERVE_BYTES,
    described,
    disks_command,
    in_gib,
    largest_archive,
    parse_disks,
    room_refusal,
)
from .abstract_executor import AbstractSSHExecutor
from .cloud_restore_upload_grant import (
    GRANT_TOKEN,
    RRSYNC,
    SAFE_UPLOAD_NAME,
    UPLOAD_ROOT,
)

_logger = logging.getLogger(__name__)

#: Where the ``rsync`` package keeps a copy of ``rrsync`` when it does
#: not install one in ``/usr/bin``, newest layout first. Older releases
#: only carried it as documentation, unpacked or compressed.
_RRSYNC_SOURCES = (
    "/usr/share/rsync/scripts/rrsync",
    "/usr/share/doc/rsync/scripts/rrsync",
    "/usr/share/doc/rsync/scripts/rrsync.gz",
)


def _ensure_rrsync(target=RRSYNC, sources=_RRSYNC_SOURCES):
    """Return the shell that settles the key's wrapper, or refuses to.

    Only the rrsync of rsync 3.2.4 and later will do: the key's forced
    command passes it ``-munge`` and ``-no-del``
    (``cloud.restore.upload.grant._authorized_keys_line``), and the Perl
    rrsync before it has neither. A copy the package ships is installed
    only if its source declares both options; the wrapper in place,
    whoever put it there, must name both in its usage.

    Installed as root: directly when the account is root — the
    platform's hosts, which may have no ``sudo`` — and with ``sudo -n``
    otherwise, which fails rather than waits for a password.

    Prints the wrapper's path and exits 0. Otherwise prints ``OLD`` (an
    rrsync without those options), ``MISSING`` (none at all),
    ``NOSUDO`` (a copy that would do, which an account other than root
    could not install: no ``sudo`` without a password) or ``FAILED``
    (one root could not install), says what to do on stderr and exits 1.

    :param str target: where the forced command expects the wrapper
    :param sources: the copies to install it from, in order of preference
    :rtype: str
    """
    wrapper = shlex.quote(target)
    copies = shlex.join(sources)
    return "\n".join([
        # Whether its input holds both words.
        'both() { t=$(cat); case "$t" in *"$1"*) ;; *) return 1 ;; esac;'
        ' case "$t" in *"$2"*) return 0 ;; esac; return 1; }',
        'unpack() { case "$1" in *.gz) gzip -dc -- "$1" ;;'
        ' *) cat -- "$1" ;; esac; }',
        # ``-n``: an account whose sudo asks for a password fails here
        # rather than waiting for one.
        'as_root() { if [ "$(id -u)" -eq 0 ]; then "$@";'
        ' else sudo -n "$@"; fi; }',
        "seen='' copy=''",
        f"if [ ! -x {wrapper} ]; then",
        f"  for src in {copies}; do",
        '    [ -f "$src" ] || continue',
        "    seen=1",
        """    unpack "$src" | both "'-munge'" "'-no-del'" || continue""",
        '    copy=$src',
        f'    unpack "$src" | as_root tee {wrapper} > /dev/null'
        f" && as_root chmod 755 {wrapper} && break",
        "  done",
        "fi",
        f"if [ -x {wrapper} ] && {wrapper} -help 2>&1 | both -munge -no-del;"
        f" then echo {wrapper}",
        # A copy that would do, which could not be put in place: the
        # cause is the account's rights, not the host's rsync.
        'elif [ -n "$copy" ] && [ "$(id -u)" -ne 0 ]; then echo NOSUDO;'
        ' echo "The rsync package of this host ships an rrsync that would'
        ' do ($copy), but the account this panel connects as'
        ' ($(id -un)) cannot install it: it cannot run sudo without a'
        f' password. Install it as root at {target} (mode 755), or let'
        ' this account run sudo without a password, then ask for the'
        ' upload key again." >&2; exit 1',
        'elif [ -n "$copy" ]; then echo FAILED;'
        f' echo "The rrsync of this host ($copy) could not be installed at'
        f' {target}: the error above says why." >&2; exit 1',
        f'elif [ -e {wrapper} ] || [ -n "$seen" ]; then echo OLD;'
        " echo 'This host has an rrsync older than rsync 3.2.4: it has"
        " neither -munge nor -no-del, and an upload through it could write"
        " outside its directory. No upload key was installed. Update rsync"
        " to 3.2.4 or later, or restore from a link.' >&2; exit 1",
        "else echo MISSING;"
        " echo 'This host has no rrsync, so a restricted upload key cannot"
        " be installed. Install rsync 3.2.4 or later, or restore from a"
        " link.' >&2; exit 1",
        "fi",
    ])


#: Whole lines an install before 1.0.200 left in ``authorized_keys``:
#: its key line was written between double quotes, so the shell split it
#: at the quotes it holds, and only the last piece carried the grant's
#: marker. No key, and no line anyone writes by hand; escaped for ``sed``.
_STRAY_LINES = (
    f"command={RRSYNC}".replace("/", r"\/"), "-wo", "-munge", "-no-del",
)

#: A line of the upload's listing: size, SHA-256 and name.
_LISTED = re.compile(r"(\d+) ([0-9a-f]{64}) (\S+)")


def authorized_keys_path():
    """Return the shell expression naming the acting user's key file."""
    return "$HOME/.ssh/authorized_keys"


class GrantRestoreUploadKeyExecutor(AbstractSSHExecutor):
    """Install the grant's public key, restricted to its own directory."""

    _job_type = "grant_restore_upload_key"

    # The grant's host, named on purpose: the key, the upload and its
    # directory are there, wherever the instance lives when this runs.
    _pinned_to_host = True

    def _grant(self):
        """Return the grant this job acts on."""
        return self.env["cloud.restore.upload.grant"].browse(
            self.job.payload.get("grant_id"),
        )

    #: Why the door cannot open, when the disks leave no room for it.
    _refusal = ""

    async def before_execute(self, transport):
        """Measure the host's disks, and set the door's size limit from them.

        The largest archive the disks could restore, with the copy the
        restore makes of it out of the door (``restore_space``): what no
        file sent through the door may pass, and what the watch of the
        doors holds its directory to (``_watch_doors``). Not raised: a
        door the disks leave no room for is refused by the first step,
        and ``on_failure`` closes it.

        :param transport: the job's open transport
        """
        grant = self._grant()
        if not grant.exists():
            return
        answer = await transport.run(
            disks_command(posixpath.dirname(UPLOAD_ROOT)),
        )
        disks = parse_disks(answer.stdout) if answer.exit_status == 0 else None
        if disks is None:
            self._refusal = (
                "The free disk on the server could not be measured: no "
                "upload door was opened."
            )
            return
        limit = largest_archive(disks, on_job_disk=2)
        if not limit:
            self._refusal = (
                room_refusal(disks, 1, 1, "an upload")
                or "Not enough free disk on the server for an upload: no "
                   "upload door was opened."
            )
            return
        grant.sudo().upload_limit_bytes = limit
        self._sys(
            f"An upload through this door may be at most {in_gib(limit)} "
            f"({described(disks)})."
        )

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Grant is gone", "echo 'grant no longer exists'; false")]
        grant._check_job(self.job)
        if self._refusal or not grant.upload_limit_bytes:
            return [(
                "Measure free disk",
                f"echo {shlex.quote(self._refusal or 'No upload limit.')} >&2;"
                " false",
                {"stop_on_failure": True},
            )]
        keys = authorized_keys_path()
        line = grant._authorized_keys_line()
        return [
            (
                # Below a root this account owns, 0700: a root another
                # account created first (its name is fixed) would let it
                # swap the grant's directory for one of its own, or put a
                # link in place of the archive. Refused rather than used.
                # The grant's own directory may be there from a retry.
                # The usual root left by another account is root's own,
                # after a hardening moved the panel to an operator
                # account: the message says what to remove.
                # The root's 0700 is also what keeps a device node or a
                # setuid file out of other accounts' reach: rrsync lets the
                # uploader keep such bits, and where the panel connects as
                # root they hold. The restore takes only a regular file,
                # with a plain ``cp``, which keeps no setuid bit
                # (``_take_upload``).
                "Prepare the upload directory",
                f"root={shlex.quote(UPLOAD_ROOT)};"
                f" dir={shlex.quote(grant._directory())};"
                ' { [ -e "$root" ] || [ -L "$root" ] || mkdir -m 700 "$root"; }'
                ' && [ -d "$root" ] && [ ! -L "$root" ] && [ -O "$root" ]'
                ' && chmod 700 "$root"'
                ' && { { [ -d "$dir" ] && [ ! -L "$dir" ] && [ -O "$dir" ]; }'
                f' || mkdir -m 700 {shlex.quote(grant._directory())}; }}'
                ' || { echo "$root belongs to'
                ' $(stat -c %U -- "$root" 2>/dev/null || echo nobody),'
                ' not to $(id -un), the account this panel connects as,'
                ' or is not a plain directory: remove it as root'
                ' (sudo rm -rf $root) and ask for the upload key again" >&2;'
                ' exit 1; }',
                # Neither step may fail and the key still be installed.
                {"stop_on_failure": True},
            ),
            (
                # Resolved rather than assumed: without the wrapper the
                # only way to accept the upload would be an unrestricted
                # key, and that is not a trade this makes silently. Nor
                # is an rrsync that cannot keep a link from leading out
                # of the directory (``_ensure_rrsync``).
                "Resolve the restricted rsync wrapper",
                _ensure_rrsync(),
                {"stop_on_failure": True},
            ),
            (
                "Install the one-use key",
                f"set -e; mkdir -p $HOME/.ssh; touch {keys};"
                f" chmod 600 {keys};"
                # Remove any previous line for this grant first, so a
                # retry cannot leave two.
                f" sed -i '/{grant._key_comment()}/d' {keys};"
                # One quoted word: the line holds double quotes of its
                # own, and between double quotes the shell split it into
                # several lines, none a key sshd accepts (28-oct-2026).
                f" printf '%s\\n' {shlex.quote(line)} >> {keys}",
            ),
        ]

    async def on_success(self, results):
        """Report the deadline, and fail loudly if the wrapper is missing.

        The resolving step stops the run when it settles on no wrapper;
        this is the second check, on what it printed: a key whose forced
        command does not exist would fail every upload with nothing to
        read, so the grant is closed instead.
        """
        grant = self._grant()
        resolved = (
            results.get("Resolve the restricted rsync wrapper", {})
            .get("stdout", "")
            .strip()
            .splitlines()
        )
        path = resolved[-1].strip() if resolved else ""
        if path != "/usr/bin/rrsync":
            grant.sudo().state = "revoked"
            raise ValueError(
                "This host has no rrsync wrapper, so a restricted upload "
                "key cannot be installed. Install the 'rsync' package's "
                "scripts and try again."
            )
        self._sys(
            f"✓ Upload key installed, valid until {grant.expires_at} UTC."
        )

    async def on_failure(self, results, errors):
        """Report the errors, and close a grant whose key never went in.

        A step before the key's stops the run (a root that is not this
        account's, an rrsync that is missing or too old, or one this
        account cannot install): nothing was
        installed, so the grant is closed here rather than left open
        until its deadline for an upload that cannot happen.
        """
        for err in errors:
            self._sys(f"✗ {err}")
        grant = self._grant()
        if grant.exists() and "Install the one-use key" not in results:
            grant.sudo().state = "revoked"


class VerifyRestoreUploadExecutor(AbstractSSHExecutor):
    """Report what actually landed in the grant's directory."""

    _job_type = "verify_restore_upload"

    # The grant's host, named on purpose: the key, the upload and its
    # directory are there, wherever the instance lives when this runs.
    _pinned_to_host = True

    def _grant(self):
        return self.env["cloud.restore.upload.grant"].browse(
            self.job.payload.get("grant_id"),
        )

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Grant is gone", "echo 'grant no longer exists'; false")]
        grant._check_job(self.job)
        return [(
            # One line per file: size, digest, name. Computed here rather
            # than trusted from the uploader, which is the whole point of
            # showing it before anything is restored.
            "Inspect the upload",
            f"cd {shlex.quote(grant._directory())} 2>/dev/null || exit 0;"
            " export LC_ALL=C;"
            # A link is not a file that arrived: the restore would follow
            # it to whatever it names.
            " for f in *; do [ -f \"$f\" ] && ! [ -L \"$f\" ] || continue;"
            # Nor is a name that is not one safe component
            # (``SAFE_UPLOAD_NAME``) ever printed: a newline in it would
            # print a line of this listing — any name, ``..`` included —
            # and the restore would take it for what arrived. A ``-``
            # line counts it.
            " case \"$f\" in [!A-Za-z0-9]* | *[!A-Za-z0-9._-]*)"
            " echo -; continue ;; esac;"
            " printf '%s %s %s\\n' \"$(stat -c %s -- \"$f\")\""
            " \"$(sha256sum -- \"$f\" | cut -d' ' -f1)\" \"$f\"; done",
        ), (
            # What a restore of it would have free: the disk the door's
            # directory is on, and the instances' (``restore_space``).
            "Measure free disk",
            f"{disks_command(grant._directory())} 2>/dev/null",
        )]

    async def on_success(self, results):
        """Record the last file listed, or say why there is none.

        Only a line of the listing's own shape whose name is one safe
        component counts; anything else is a file whose name the
        restore could not use, and the operator is told to send it
        again under one it can. When nothing counts, what an earlier
        check recorded is forgotten: it is not what is there now, and a
        restore must not take it for what was confirmed.
        """
        grant = self._grant()
        rows = []
        unusable = 0
        for line in results.get("Inspect the upload", {}).get(
            "stdout", "",
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            listed = _LISTED.fullmatch(line)
            if listed and SAFE_UPLOAD_NAME.fullmatch(listed.group(3)):
                rows.append(listed.groups())
            else:
                unusable += 1
        if unusable:
            self._sys(
                f"⚠ {unusable} file(s) arrived with a name that cannot be "
                "restored: only letters, digits, dots, dashes and "
                "underscores, starting with a letter or a digit. Send the "
                "archive again as restore.zip."
            )
        if not rows:
            grant.sudo().write({
                "received_filename": False,
                "received_bytes": 0,
                "received_sha256": False,
            })
            self._sys("Nothing that can be restored has arrived yet.")
            return
        size, digest, name = rows[-1]
        # Not offered for a restore the disk cannot take: the restore
        # copies it out of the door, then unpacks it (``restore_space``).
        # Forgotten as when nothing arrived, and said, not raised: a
        # failed job would take the forgetting back.
        disks = parse_disks(
            results.get("Measure free disk", {}).get("stdout", ""),
        )
        refusal = disks and room_refusal(
            disks, int(size), 1, f"{name} ({in_gib(int(size))})",
        )
        if disks is None or refusal:
            grant.sudo().write({
                "received_filename": False,
                "received_bytes": 0,
                "received_sha256": False,
            })
            if disks is None:
                self._sys(
                    f"⚠ {name} arrived ({size} bytes), but the free disk on "
                    "the server could not be measured: it cannot be "
                    "restored. Check again."
                )
            else:
                self._sys("⚠ " + refusal)
            return
        grant.sudo().write({
            "received_filename": name,
            "received_bytes": int(size),
            "received_sha256": digest,
        })
        self._sys(f"✓ Received {name} — {size} bytes, SHA-256 {digest}")


def _revoke_commands(grant, keep_directory=False, label=""):
    """Return the steps that close *grant*'s door on its host.

    The key's line goes, keyed on the grant's own marker, so an
    operator's own keys in the same file are untouched; then the whole
    lines an install before 1.0.200 left when the shell split its line
    (``_STRAY_LINES``); then, unless *keep_directory*, its directory: a
    session still open can then create no file there.

    :param grant: the ``cloud.restore.upload.grant``, its shape checked
    :param bool keep_directory: leave the directory, for a restore that
        reads it next
    :param str label: what tells these steps from another door's, in one
        job that closes several
    :return: executor command tuples
    """
    comment = grant._key_comment()
    if not re.fullmatch(r"ic-restore-[0-9a-f]{16}", comment):
        return [(f"Refusing to edit keys{label}", "false")]
    commands = [(
        f"Remove the one-use key{label}",
        f"sed -i -e '/{comment}/d'"
        + "".join(f" -e '/^{stray}$/d'" for stray in _STRAY_LINES)
        + f" {authorized_keys_path()} || true",
    )]
    if not keep_directory:
        commands.append((
            f"Remove the upload directory{label}",
            f"rm -rf {shlex.quote(grant._directory())} || true",
        ))
    return commands


class RevokeRestoreUploadKeyExecutor(AbstractSSHExecutor):
    """Remove the grant's key line and its directory from the host."""

    _job_type = "revoke_restore_upload_key"

    # The grant's host, named on purpose: the key, the upload and its
    # directory are there, wherever the instance lives when this runs.
    _pinned_to_host = True

    def _grant(self):
        return self.env["cloud.restore.upload.grant"].browse(
            self.job.payload.get("grant_id"),
        )

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Nothing to revoke", "true")]
        grant._check_job(self.job)
        # A restore consuming the archive revokes the key first and reads
        # the file afterwards, so it asks for the directory to stay. The
        # sweep and the operator do not.
        return _revoke_commands(
            grant, keep_directory=(self.job.payload or {}).get("keep_directory"),
        )

    async def on_success(self, results):
        self._sys("✓ Upload key revoked and directory removed.")


class WatchRestoreUploadsExecutor(AbstractSSHExecutor):
    """Close the open doors of a host that hold too much, or the disk needs.

    The key's file size limit bounds each file a door writes; many files,
    or the doors of many instances on one host, still add up. Every few
    minutes (``cloud.restore.upload.grant._watch_doors``) this measures
    each open door's directory (``du``) and the disks (``restore_space``),
    and closes, there and then rather than through the instance's queue:

    * a door whose directory holds more than its limit
      (``upload_limit_bytes``; for a door opened before the limits were
      kept, the largest upload the disks leave now);
    * every door of the host, when the doors write on the disk the
      instances are kept on and it is down to the reserve.

    A door is closed as its revocation does (``_revoke_commands``). What
    cannot be measured closes nothing: the key's own limit and deadline
    still hold.
    """

    _job_type = "watch_restore_uploads"

    #: A line of the measurement: ``door <token> <bytes>``.
    _DOOR_LINE = re.compile(r"door ([0-9a-f]{16})(?: (\d+))?")

    def _doors(self):
        """Return the host's open doors whose shape is the panel's own.

        :rtype: cloud.restore.upload.grant
        """
        return self.env["cloud.restore.upload.grant"].sudo().search([
            ("host_id", "=", self.job.host_id.id),
            ("state", "=", "granted"),
        ]).filtered(lambda door: GRANT_TOKEN.fullmatch(door.token or ""))

    def _measure_command(self, doors):
        """Return the shell that prints the disks, then each door's bytes.

        :param doors: the doors to measure
        :rtype: str
        """
        sizes = "".join(
            f"; printf 'door %s %s\\n' {door.token}"
            f' "$(du -sb -- {shlex.quote(door._directory())} 2>/dev/null'
            " | cut -f1)\""
            for door in doors
        )
        return f"{{ {disks_command(posixpath.dirname(UPLOAD_ROOT))}; }} 2>/dev/null{sizes}"

    def _over(self, doors, stdout):
        """Return the *doors* to close, and why, from what the host printed.

        :param doors: the doors measured
        :param str stdout: what ``_measure_command`` printed
        :return: ``{door: reason}``
        :rtype: dict
        """
        lines = (stdout or "").splitlines()
        held = {}
        for line in lines:
            found = self._DOOR_LINE.fullmatch(line.strip())
            if found:
                held[found.group(1)] = int(found.group(2) or 0)
        disks = parse_disks("\n".join(
            line for line in lines if not line.startswith("door ")
        ))
        closing = {}
        for door in doors:
            # A door opened before the limits were kept is held to what
            # the disks leave now.
            limit = int(door.upload_limit_bytes or 0) or (
                disks and largest_archive(disks, on_job_disk=2)
            )
            used = held.get(door.token)
            if used is not None and limit is not None and used > limit:
                closing[door] = (
                    f"it holds {in_gib(used)}, more than its limit of "
                    f"{in_gib(limit)}"
                )
            elif disks and disks.shared and disks.free() < RESERVE_BYTES:
                closing[door] = (
                    f"the disk of the instances has {in_gib(disks.free())} "
                    f"free, less than the {in_gib(RESERVE_BYTES)} kept for "
                    "them"
                )
        return closing

    async def before_execute(self, transport):
        """Measure the open doors and the disks; decide which doors close.

        :param transport: the job's open transport
        """
        self._closing = {}
        doors = self._doors()
        if not doors:
            return
        answer = await transport.run(self._measure_command(doors))
        self._closing = self._over(doors, answer.stdout)

    def get_commands(self):
        commands = []
        for door, _reason in getattr(self, "_closing", {}).items():
            door._check_shape()
            commands += _revoke_commands(door, label=f" ({door.token})")
        return commands or [("Nothing to close", "true")]

    async def on_success(self, results):
        """Record the doors closed, and say why each was."""
        closing = getattr(self, "_closing", {})
        for door, reason in closing.items():
            door.sudo().state = "revoked"
            _logger.warning(
                "restore upload door %s of %s closed: %s",
                door.token, door.instance_id.name, reason,
            )
            self._sys(
                f"✓ Closed the upload door of {door.instance_id.name}: "
                f"{reason}."
            )
        if not closing:
            self._sys("✓ The open upload doors are within their limits.")
