"""Install, verify and remove the one-use upload key on a host.

Three small jobs around ``cloud.restore.upload.grant``: one opens the
door, one says what came through it, one closes it. They are separate
because the middle one is the operator's decision point — nothing is
restored until they have seen the name, the size and the digest of what
actually landed, computed on the host rather than promised by the
browser that sent it.
"""
import asyncio
import logging
import posixpath
import re
import shlex
import time

from ..restore_space import (
    RESERVE_BYTES,
    STAGING_ROOT,
    described,
    disks_command,
    in_gib,
    largest_archive,
    parse_disks,
    room_refusal,
    room_shortfall,
)
from .abstract_executor import AbstractSSHExecutor
from .cloud_restore_upload_grant import (
    DOOR_COMMAND,
    GRANT_TOKEN,
    HOST_DOORS_LOCK_NAMESPACE,
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

#: A line of what a door holds (``doors_measure_command``):
#: ``door <token> <bytes>``.
_DOOR_LINE = re.compile(r"door ([0-9a-f]{16})(?: (\d+))?")

#: What the step that refuses a root of another account prints
#: (``GrantRestoreUploadKeyExecutor.get_commands``).
_FOREIGN_ROOT = re.compile(r"(\S+) belongs to (\S+), not to (\S+), the account")

#: How long the job that opens a door waits for the lock of its host's
#: doors, in seconds, and how long the measure it makes under that lock
#: may take: another job holding the lock holds it while it measures, so
#: the wait is bounded by the measure, and a host that stopped answering
#: holds no other door's job for the hour of an SSH command's default
#: (the review of 18-nov-2026, L3).
HOST_DOORS_LOCK_WAIT_S = 150
DOORS_MEASURE_TIMEOUT_S = 120


def doors_measure_command(doors):
    """Return the shell that prints the disks of the doors, then what each holds.

    The disks as ``restore_space.disks_command`` prints them, for the
    doors' root, then one line ``door <token> <bytes>`` a door, its bytes
    left out when its directory cannot be read. Without doors, the disks
    alone.

    :param doors: ``(token, directory)`` of each door to measure
    :rtype: str
    """
    disks = disks_command(posixpath.dirname(UPLOAD_ROOT))
    if not doors:
        return disks
    sizes = "".join(
        f"; printf 'door %s %s\\n' {token}"
        f' "$(du -sb -- {shlex.quote(directory)} 2>/dev/null'
        " | cut -f1)\""
        for token, directory in doors
    )
    return f"{{ {disks}; }} 2>/dev/null{sizes}"


def parse_doors_measure(stdout):
    """Return what :func:`doors_measure_command` printed.

    :param str stdout: what it printed
    :return: ``(disks, held)``: the ``restore_space.Disks``, or None when
        they could not be read, and ``{token: bytes}`` of each door
        measured, 0 for one whose directory could not be read
    :rtype: tuple
    """
    lines = (stdout or "").splitlines()
    held = {}
    for line in lines:
        found = _DOOR_LINE.fullmatch(line.strip())
        if found:
            held[found.group(1)] = int(found.group(2) or 0)
    disks = parse_disks("\n".join(
        line for line in lines if not line.startswith("door ")
    ))
    return disks, held


def room_notice(env, short, what):
    """Return, in *env*'s language, why *what* finds no room on the host.

    :param env: an environment in the language of the door's owner
        (``cloud.restore.upload.grant._in_lang``)
    :param tuple short: what ``restore_space.room_shortfall`` answered
    :param str what: the archive, as the notice names it
    :rtype: str
    """
    where, free, needed = short
    values = {
        "what": what, "free": in_gib(free), "needed": in_gib(needed),
        "reserve": in_gib(RESERVE_BYTES), "dir": STAGING_ROOT,
    }
    if where == "staging":
        return env._(
            "Not enough free disk on the server for %(what)s in %(dir)s: "
            "%(free)s free, %(needed)s needed (its copy, and %(reserve)s kept "
            "free there). Free space on the server, or restore a smaller "
            "archive.",
            **values,
        )
    if where == "docker":
        return env._(
            "Not enough free disk on the server for %(what)s on the disk its "
            "instances are kept on: %(free)s free, %(needed)s needed (twice "
            "its size to unpack it, and %(reserve)s the server's other "
            "instances keep). Free space on the server, or restore a smaller "
            "archive.",
            **values,
        )
    return env._(
        "Not enough free disk on the server for %(what)s: %(free)s free, "
        "%(needed)s needed (the archive, twice its size to unpack it, and "
        "%(reserve)s the server's other instances keep). Free space on the "
        "server, or restore a smaller archive.",
        **values,
    )


def authorized_keys_path():
    """Return the shell expression naming the acting user's key file."""
    return "$HOME/.ssh/authorized_keys"


def door_limit_message(limit, disks, pending=0, doors=0):
    """Return what the log of the job that opens a door says of its limit.

    :param int limit: the most the door takes, in bytes
    :param restore_space.Disks disks: what the host had free
    :param int pending: what the host's other open doors may still receive
    :param int doors: how many those are
    :rtype: str
    """
    others = (
        f", less {in_gib(pending)} its {doors} other open door(s) may still "
        "receive" if doors else ""
    )
    return (
        f"An upload through this door may be at most {in_gib(limit)} "
        f"({described(disks)}{others}): the largest archive the server's "
        "free disk can restore now. The door takes one file, restore.zip, "
        "sent with rsync --inplace; a larger file stops at that size."
    )


def cut_at_limit(size, limit):
    """Whether a file of *size* bytes stopped at a door's limit of *limit*.

    The key's ``ulimit -f`` counts in KiB (``forced_command``): a write
    past it fails, and ``rsync --inplace`` leaves the file at exactly the
    limit, rounded down to a KiB. A file that size is taken for one cut
    there: one that is it by chance would be a byte short of not fitting.

    :param int size: the file's size
    :param int limit: the door's ``upload_limit_bytes``, 0 when unknown
    :rtype: bool
    """
    return bool(limit) and int(size) >= int(limit) // 1024 * 1024


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

    #: Why the door cannot open, when the disks leave no room for it: in
    #: English for the job's log, and in the language of the door's owner
    #: for the Import dialog (``notice``).
    _refusal = ""
    _refusal_notice = ""

    #: The last line each step wrote on stderr, for the notice of a step
    #: that stopped the run (``on_failure``).
    _stderr_of = None

    async def on_stderr(self, line):
        """Log *line*, and keep it as the last its step said.

        :param str line: a line the running step wrote on stderr
        """
        await super().on_stderr(line)
        if line.strip():
            if self._stderr_of is None:
                self._stderr_of = {}
            self._stderr_of[getattr(self, "_running_step", None)] = line.strip()

    def _refuse(self, english, notice):
        """Refuse the door: the first step says *english*, the dialog *notice*.

        :param str english: for the job's log
        :param str notice: in the language of the door's owner
        """
        self._refusal = english
        self._refusal_notice = notice

    async def before_execute(self, transport):
        """Measure the host's disks, and set the door's share of them.

        The largest archive the disks could restore, with the copy the
        restore makes of it out of the door (``restore_space``), less what
        the host's other open doors may still receive, each up to its own
        limit: what the one file the door takes may not pass
        (``restore_door.sh``), and what the watch of the doors holds its
        directory to (``_watch_doors``). Under a lock of the host's doors,
        held until the limit is recorded: two doors of the host opening
        at once are measured one after the other, so they cannot both
        count the same free disk. Not raised: a door the disks leave no
        room for is refused by the first step, and ``on_failure`` closes
        it.

        :param transport: the job's open transport
        """
        grant = self._grant()
        if not grant.exists():
            return
        speaks = grant._in_lang()
        host_id = grant.host_id.id
        # The lock is held, and the other doors read, on a durable cursor
        # of its own (READ COMMITTED): a door another job recorded a
        # moment ago is not in this job's snapshot. It writes nothing, so
        # ``_persist`` below, which commits the limit on a cursor of its
        # own while the lock is still held, contends for no row with it.
        with self._durable_env() as env:
            cr = env.cr
            if not await self._lock_host_doors(cr, host_id):
                self._refuse(
                    "Another upload door of this server was being opened "
                    "and its measure of the disk did not end: no upload door "
                    "was opened.",
                    speaks._(
                        "Another upload door of this server was being opened "
                        "and its measure of the disk did not end: no upload "
                        "door was opened. Ask for the upload key again in a "
                        "few minutes."
                    ),
                )
                return
            # A door closed in the panel whose key may still take an
            # upload counts as open until the job that removes the key
            # has done so (``key_removed``; the review of 18-nov-2026,
            # M1): until its deadline at most, when OpenSSH stops
            # accepting the key.
            cr.execute(
                "SELECT token, upload_limit_bytes"
                " FROM cloud_restore_upload_grant"
                " WHERE host_id = %s AND id != %s"
                " AND (state = 'granted' OR key_removed IS NOT TRUE)"
                " AND expires_at > now() AT TIME ZONE 'UTC'"
                " AND upload_limit_bytes > 0",
                (host_id, grant.id),
            )
            others = [
                (token, int(limit)) for token, limit in cr.fetchall()
                if GRANT_TOKEN.fullmatch(token or "")
            ]
            answer = await transport.run(doors_measure_command([
                (token, f"{UPLOAD_ROOT}/{token}") for token, _limit in others
            ]), timeout=DOORS_MEASURE_TIMEOUT_S)
            disks, held = parse_doors_measure(answer.stdout)
            if disks is None:
                self._refuse(
                    "The free disk on the server could not be measured: no "
                    "upload door was opened.",
                    speaks._(
                        "The free disk on the server could not be measured: "
                        "no upload door was opened. Ask for the upload key "
                        "again."
                    ),
                )
                return
            pending = sum(
                max(0, limit - held.get(token, 0)) for token, limit in others
            )
            room = disks._replace(
                job=max(0, disks.job - pending),
                docker=max(0, disks.docker - pending)
                if disks.shared else disks.docker,
            )
            limit = largest_archive(room, on_job_disk=2)
            if not limit:
                self._refuse_no_room(speaks, disks, room, pending, len(others))
                return
            self._persist(grant.sudo(), {"upload_limit_bytes": limit})
        self._sys(door_limit_message(limit, disks, pending, len(others)))

    async def _lock_host_doors(self, cr, host_id):
        """Take the lock of *host_id*'s doors on *cr*, waiting a bounded time.

        Tried again every second rather than waited on: the job holding
        it measures its host over SSH (``DOORS_MEASURE_TIMEOUT_S``), and a
        job of another door of the host must not wait on it unbounded.

        :param cr: the cursor whose transaction holds the lock
        :param int host_id: the host whose doors are measured
        :return: whether the lock was taken within
            ``HOST_DOORS_LOCK_WAIT_S``
        :rtype: bool
        """
        deadline = time.monotonic() + HOST_DOORS_LOCK_WAIT_S
        while True:
            cr.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (HOST_DOORS_LOCK_NAMESPACE, host_id),
            )
            if cr.fetchone()[0]:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(1)

    def _refuse_no_room(self, speaks, disks, room, pending, doors):
        """Refuse a door the disks, less the other doors' shares, leave no room for.

        The other doors are named only when without them there would be
        room: a disk too full for any door says so.

        :param speaks: an environment in the language of the door's owner
        :param restore_space.Disks disks: what the disks have free
        :param restore_space.Disks room: what the disks have free, less
            what the other open doors may still receive
        :param int pending: that much
        :param int doors: how many doors those are
        """
        if doors and largest_archive(disks, on_job_disk=2):
            self._refuse(
                f"Not enough free disk on the server for another upload "
                f"door: its {doors} other open door(s) may still receive "
                f"{in_gib(pending)}, and what is left is not enough. Close "
                "one, or wait until it closes, then ask again.",
                speaks._(
                    "Not enough free disk on the server for another upload "
                    "door: its %(doors)s other open door(s) may still receive "
                    "%(pending)s, and what is left is not enough. Close one, "
                    "or wait until it closes, then ask for the upload key "
                    "again.",
                    doors=doors, pending=in_gib(pending),
                ),
            )
            return
        short = room_shortfall(room, 1, 1)
        self._refuse(
            room_refusal(room, 1, 1, "an upload")
            or "Not enough free disk on the server for an upload: no upload "
               "door was opened.",
            room_notice(speaks, short, speaks._("an upload")) if short
            else speaks._(
                "Not enough free disk on the server for an upload: no upload "
                "door was opened."
            ),
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
                # In the root just checked: the check that holds the door
                # to one file, which the key runs before the wrapper
                # (``forced_command``). Replaced in one rename, so a door
                # already open keeps the copy it runs.
                "Install the door's command",
                self.run_script("restore_door.sh", ["install", DOOR_COMMAND]),
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
        grant.sudo().notice = False
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
            # No key went in: none to remove (``key_removed``).
            grant.sudo().write({
                "state": "revoked",
                "key_removed": True,
                "notice": self._failure_notice(grant, results),
            })

    def _failure_notice(self, grant, results):
        """Return why the door did not open, as the step that stopped it said.

        In the language of the door's owner, for the Import dialog: the
        steps' own words where the panel knows them, the line the step
        wrote last where it does not, and the job's log otherwise. No
        step prints a secret: the key's line is the last step's.

        :param grant: the ``cloud.restore.upload.grant``
        :param dict results: the steps that ran, by label
        :rtype: str
        """
        speaks = grant._in_lang()
        if self._refusal_notice:
            return self._refusal_notice
        failed = next((
            label for label, data in reversed(list(results.items()))
            if data.get("exit_status", 1) != 0
        ), None)
        said = (self._stderr_of or {}).get(failed, "")
        if failed == "Prepare the upload directory":
            found = _FOREIGN_ROOT.search(said)
            if found:
                return speaks._(
                    "%(root)s belongs to %(owner)s, not to %(account)s, the "
                    "account this panel connects as, or is not a plain "
                    "directory: remove it as root (sudo rm -rf %(root)s) and "
                    "ask for the upload key again.",
                    root=found.group(1), owner=found.group(2),
                    account=found.group(3),
                )
        elif failed == "Install the door's command":
            return speaks._(
                "The upload door's command could not be installed in "
                "%(root)s: the log of the job that installs its key says why.",
                root=UPLOAD_ROOT,
            )
        elif failed == "Resolve the restricted rsync wrapper":
            printed = (results[failed].get("stdout") or "").split()
            known = {
                "OLD": speaks._(
                    "This server has an rrsync older than rsync 3.2.4, through "
                    "which an upload could write outside its directory: no "
                    "upload door was opened. Update rsync to 3.2.4 or later, "
                    "or restore from a link."
                ),
                "MISSING": speaks._(
                    "This server has no rrsync, so a restricted upload key "
                    "cannot be installed. Install rsync 3.2.4 or later, or "
                    "restore from a link."
                ),
                "NOSUDO": speaks._(
                    "The rsync package of this server ships an rrsync that "
                    "would do, but the account this panel connects as cannot "
                    "install it: it cannot run sudo without a password. "
                    "Install it as root at %(path)s (mode 755), or let this "
                    "account run sudo without a password, then ask for the "
                    "upload key again.",
                    path=RRSYNC,
                ),
                "FAILED": speaks._(
                    "The rrsync of this server could not be installed at "
                    "%(path)s: the log of the job that installs its key says "
                    "why.",
                    path=RRSYNC,
                ),
            }
            if printed and printed[-1] in known:
                return known[printed[-1]]
        return said or speaks._(
            "The upload door did not open: the log of the job that installs "
            "its key says why."
        )


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
            # Read without waiting for a writer (``iflag=nonblock``): a
            # pipe put in its place after the test above, which the door
            # no longer lets through (``-D``, ``restore_door.sh``), ends
            # the read at once rather than holding the check forever,
            # and is then not a file of any size worth listing.
            " printf '%s %s %s\\n' \"$(stat -c %s -- \"$f\")\""
            " \"$(dd if=\"$f\" iflag=nonblock bs=1M status=none"
            " | sha256sum | cut -d' ' -f1)\" \"$f\"; done",
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
        speaks = grant._in_lang()
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
                # The empty restore.zip the door makes before the first
                # upload (``restore_door.sh``) is not one.
                if int(listed.group(1)):
                    rows.append(listed.groups())
            else:
                unusable += 1
        unusable_note = ""
        if unusable:
            self._sys(
                f"⚠ {unusable} file(s) arrived with a name that cannot be "
                "restored: only letters, digits, dots, dashes and "
                "underscores, starting with a letter or a digit. Send the "
                "archive again as restore.zip."
            )
            unusable_note = speaks._(
                "%(count)s file(s) arrived with a name that cannot be "
                "restored: only letters, digits, dots, dashes and "
                "underscores, starting with a letter or a digit. Send the "
                "archive again as restore.zip.",
                count=unusable,
            )
        if not rows:
            self._forget_received(grant, unusable_note)
            self._sys("Nothing that can be restored has arrived yet.")
            return
        size, digest, name = rows[-1]
        limit = int(grant.upload_limit_bytes or 0)
        if cut_at_limit(size, limit):
            # The key's file size limit stopped it (``forced_command``):
            # what arrived is the start of a larger archive.
            self._sys(
                f"⚠ {name} stopped at {in_gib(int(size))}, the most this "
                f"door takes ({in_gib(limit)}, its share of the server's "
                "free disk when it opened): the archive is larger, and what "
                "arrived cannot be restored. Free space on the server and "
                "open another door, or restore a smaller archive."
            )
            self._forget_received(grant, speaks._(
                "%(name)s stopped at %(size)s, the most this door takes "
                "(%(limit)s, its share of the server's free disk when it "
                "opened): the archive is larger, and what arrived cannot be "
                "restored. Free space on the server and open another door, or "
                "restore a smaller archive.",
                name=name, size=in_gib(int(size)), limit=in_gib(limit),
            ))
            return
        # Not offered for a restore the disk cannot take: the restore
        # copies it out of the door, then unpacks it (``restore_space``).
        # Forgotten as when nothing arrived, and said, not raised: a
        # failed job would take the forgetting back.
        disks = parse_disks(
            results.get("Measure free disk", {}).get("stdout", ""),
        )
        what = f"{name} ({in_gib(int(size))})"
        if disks is None:
            self._sys(
                f"⚠ {name} arrived ({size} bytes), but the free disk on the "
                "server could not be measured: it cannot be restored. Check "
                "again."
            )
            self._forget_received(grant, speaks._(
                "%(name)s arrived (%(size)s bytes), but the free disk on the "
                "server could not be measured: it cannot be restored. Check "
                "again.",
                name=name, size=size,
            ))
            return
        short = room_shortfall(disks, int(size), 1)
        if short:
            self._sys("⚠ " + room_refusal(disks, int(size), 1, what))
            self._forget_received(grant, room_notice(speaks, short, what))
            return
        grant.sudo().write({
            "received_filename": name,
            "received_bytes": int(size),
            "received_sha256": digest,
            "notice": False,
        })
        self._sys(f"✓ Received {name} — {size} bytes, SHA-256 {digest}")

    def _forget_received(self, grant, note):
        """Forget what an earlier check recorded, and keep why for the dialog.

        Called from ``on_success`` only, so it writes on the hook's durable
        cursor (``executor_write_rules.HOOK_HELPERS``).

        :param grant: the ``cloud.restore.upload.grant`` checked
        :param str note: why nothing it holds can be restored, or ``''``
        """
        grant.sudo().write({
            "received_filename": False,
            "received_bytes": 0,
            "received_sha256": False,
            "notice": note or False,
        })


def end_door_sessions_command(directory):
    """Return the shell that ends the sessions still open in a door.

    Removing a key from ``authorized_keys`` lets its open sessions run
    on. Each is an ``rsync`` of the account the panel connects as (or
    the ``rrsync`` that starts it), working in the door's *directory*
    (``restore_door.sh`` and ``rrsync`` change to it), or in a directory
    below it: those, and no other process, are sent TERM. Read from
    ``/proc``, which every Linux host has; never fails.

    :param str directory: the door's directory
    :rtype: str
    """
    return (
        f"d={shlex.quote(directory)};"
        ' for p in /proc/[0-9]*; do [ -O "$p" ] || continue;'
        ' case "$(cat "$p/comm" 2>/dev/null)" in rsync | rrsync) ;;'
        " *) continue ;; esac;"
        ' c=$(readlink "$p/cwd" 2>/dev/null) || continue;'
        ' case "$c" in "$d" | "$d"/*) kill -- "${p#/proc/}" 2>/dev/null ;; esac;'
        " done; true"
    )


def _revoke_commands(grant, keep_directory=False, label=""):
    """Return the steps that close *grant*'s door on its host.

    The key's line goes, keyed on the grant's own marker, so an
    operator's own keys in the same file are untouched; then the whole
    lines an install before 1.0.200 left when the shell split its line
    (``_STRAY_LINES``); then the sessions of the key still open end: the
    account's ``rsync`` processes working in the door's directory, which
    removing the key leaves running (the review of 18-nov-2026, H1);
    then, unless *keep_directory*, its directory: a session still open
    can then create no file there.

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
    ), (
        f"End the door's sessions{label}",
        end_door_sessions_command(grant._directory()),
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
        """Record that the key is gone, so the door counts no more.

        Until then a door closed in the panel still counts in its host's
        free disk and in the watch (``key_removed``).
        """
        grant = self._grant()
        if grant.exists():
            grant.sudo().key_removed = True
        self._sys("✓ Upload key revoked and directory removed.")


class WatchRestoreUploadsExecutor(AbstractSSHExecutor):
    """Close the open doors of a host that hold too much, or the disk needs.

    A door takes one file, under the key's file size limit, its share of
    the host's free disk (``restore_door.sh``, ``forced_command``): that
    bounds what it holds. This is the second line behind it, for what the
    shares did not foresee (the instances' own growth while doors are
    open) and for a door opened before the bound. Every few
    minutes (``cloud.restore.upload.grant._watch_doors``) this measures
    each open door's directory (``du``) and the disks (``restore_space``),
    and closes, there and then rather than through the instance's queue:

    * a door whose directory holds more than its limit
      (``upload_limit_bytes``; for a door opened before the limits were
      kept, the largest upload the disks leave now);
    * every door of the host, when the doors write on the disk the
      instances are kept on and it is down to the reserve.

    A door is closed as its revocation does (``_revoke_commands``). A
    door closed in the panel whose key has not been removed yet is
    watched as an open one (``_key_may_work_domain``), and closing it
    removes its key; one a restore consumed keeps its directory. What
    cannot be measured closes nothing: the key's own limit and deadline
    still hold.
    """

    _job_type = "watch_restore_uploads"

    def _doors(self):
        """Return the host's doors whose key may take an upload, of the panel's shape.

        The open ones, and those closed in the panel whose key has not
        been removed yet (``_key_may_work_domain``).

        :rtype: cloud.restore.upload.grant
        """
        Grant = self.env["cloud.restore.upload.grant"].sudo()
        return Grant.search(
            [("host_id", "=", self.job.host_id.id)]
            + Grant._key_may_work_domain()
        ).filtered(lambda door: GRANT_TOKEN.fullmatch(door.token or ""))

    def _measure_command(self, doors):
        """Return the shell that prints the disks, then each door's bytes.

        :param doors: the doors to measure
        :rtype: str
        """
        return doors_measure_command(
            [(door.token, door._directory()) for door in doors],
        )

    def _over(self, doors, stdout):
        """Return the *doors* to close, and why, from what the host printed.

        :param doors: the doors measured
        :param str stdout: what ``_measure_command`` printed
        :return: ``{door: reason}``
        :rtype: dict
        """
        disks, held = parse_doors_measure(stdout)
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
            # A door a restore consumed keeps its directory: a failed
            # restore gave the archive back there for its retry.
            commands += _revoke_commands(
                door, keep_directory=door.state == "used",
                label=f" ({door.token})",
            )
        return commands or [("Nothing to close", "true")]

    async def on_success(self, results):
        """Record the doors closed, and say why each was."""
        closing = getattr(self, "_closing", {})
        for door, reason in closing.items():
            closed = {"key_removed": True}
            if door.state == "granted":
                closed["state"] = "revoked"
            door.sudo().write(closed)
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
