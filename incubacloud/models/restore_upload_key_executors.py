"""Install, verify and remove the one-use upload key on a host.

Three small jobs around ``cloud.restore.upload.grant``: one opens the
door, one says what came through it, one closes it. They are separate
because the middle one is the operator's decision point — nothing is
restored until they have seen the name, the size and the digest of what
actually landed, computed on the host rather than promised by the
browser that sent it.
"""
import logging
import re
import shlex

from .abstract_executor import AbstractSSHExecutor
from .cloud_restore_upload_grant import RRSYNC, SAFE_UPLOAD_NAME, UPLOAD_ROOT

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

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Grant is gone", "echo 'grant no longer exists'; false")]
        grant._check_job(self.job)
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
        grant.sudo().write({
            "received_filename": name,
            "received_bytes": int(size),
            "received_sha256": digest,
        })
        self._sys(f"✓ Received {name} — {size} bytes, SHA-256 {digest}")


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
        comment = grant._key_comment()
        if not re.fullmatch(r"ic-restore-[0-9a-f]{16}", comment):
            return [("Refusing to edit keys", "false")]
        commands = [(
            # Keyed on the grant's own marker, so an operator's own keys
            # in the same file are untouched. Then the whole lines an
            # install before 1.0.200 left when the shell split its line
            # (``_STRAY_LINES``).
            "Remove the one-use key",
            f"sed -i -e '/{comment}/d'"
            + "".join(f" -e '/^{stray}$/d'" for stray in _STRAY_LINES)
            + f" {authorized_keys_path()} || true",
        )]
        # A restore consuming the archive revokes the key first and reads
        # the file afterwards, so it asks for the directory to stay. The
        # sweep and the operator do not.
        if not (self.job.payload or {}).get("keep_directory"):
            commands.append((
                "Remove the upload directory",
                f"rm -rf {shlex.quote(grant._directory())} || true",
            ))
        return commands

    async def on_success(self, results):
        self._sys("✓ Upload key revoked and directory removed.")
