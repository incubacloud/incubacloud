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

from .abstract_executor import AbstractSSHExecutor

_logger = logging.getLogger(__name__)

#: Where ``rrsync`` lives on a Debian/Ubuntu host, newest layout first.
#: Bookworm ships it in ``/usr/bin``; older releases only carried it as
#: documentation, unpacked and non-executable.
_RRSYNC_CANDIDATES = (
    "/usr/bin/rrsync",
    "/usr/share/rsync/scripts/rrsync",
    "/usr/share/doc/rsync/scripts/rrsync",
    "/usr/share/doc/rsync/scripts/rrsync.gz",
)

#: Shell that resolves the wrapper, unpacking the documentation copy if
#: that is all the host has. Prints the path it settled on, or nothing.
_ENSURE_RRSYNC = " ; ".join([
    "if [ -x /usr/bin/rrsync ]; then echo /usr/bin/rrsync",
    "elif [ -f /usr/share/rsync/scripts/rrsync ]; then "
    "sudo install -m 755 /usr/share/rsync/scripts/rrsync /usr/bin/rrsync "
    "&& echo /usr/bin/rrsync",
    "elif [ -f /usr/share/doc/rsync/scripts/rrsync ]; then "
    "sudo install -m 755 /usr/share/doc/rsync/scripts/rrsync /usr/bin/rrsync "
    "&& echo /usr/bin/rrsync",
    "elif [ -f /usr/share/doc/rsync/scripts/rrsync.gz ]; then "
    "sudo sh -c 'gzip -dc /usr/share/doc/rsync/scripts/rrsync.gz "
    "> /usr/bin/rrsync && chmod 755 /usr/bin/rrsync' && echo /usr/bin/rrsync",
    "else echo MISSING",
    "fi",
])


def authorized_keys_path():
    """Return the shell expression naming the acting user's key file."""
    return "$HOME/.ssh/authorized_keys"


class GrantRestoreUploadKeyExecutor(AbstractSSHExecutor):
    """Install the grant's public key, restricted to its own directory."""

    _job_type = "grant_restore_upload_key"

    def _grant(self):
        """Return the grant this job acts on."""
        return self.env["cloud.restore.upload.grant"].browse(
            self.job.payload.get("grant_id"),
        )

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Grant is gone", "echo 'grant no longer exists'; false")]
        keys = authorized_keys_path()
        line = grant._authorized_keys_line()
        return [
            (
                "Prepare the upload directory",
                f"mkdir -p {grant._directory()}"
                f" && chmod 700 {grant._directory()}",
            ),
            (
                # Resolved rather than assumed: without the wrapper the
                # only way to accept the upload would be an unrestricted
                # key, and that is not a trade this makes silently.
                "Resolve the restricted rsync wrapper",
                _ENSURE_RRSYNC,
            ),
            (
                "Install the one-use key",
                f"set -e; mkdir -p $HOME/.ssh; touch {keys};"
                f" chmod 600 {keys};"
                # Remove any previous line for this grant first, so a
                # retry cannot leave two.
                f" sed -i '/{grant._key_comment()}/d' {keys};"
                f" printf '%s\\n' \"{line}\" >> {keys}",
            ),
        ]

    async def on_success(self, results):
        """Report the deadline, and fail loudly if the wrapper is missing.

        The install step interpolates ``{rrsync}`` from what the previous
        step resolved. When nothing resolved, the line would carry a
        forced command that does not exist — an upload that fails with
        nothing to read — so the grant is closed instead.
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
            grant.state = "revoked"
            raise ValueError(
                "This host has no rrsync wrapper, so a restricted upload "
                "key cannot be installed. Install the 'rsync' package's "
                "scripts and try again."
            )
        self._sys(
            f"✓ Upload key installed, valid until {grant.expires_at} UTC."
        )

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")


class VerifyRestoreUploadExecutor(AbstractSSHExecutor):
    """Report what actually landed in the grant's directory."""

    _job_type = "verify_restore_upload"

    def _grant(self):
        return self.env["cloud.restore.upload.grant"].browse(
            self.job.payload.get("grant_id"),
        )

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Grant is gone", "echo 'grant no longer exists'; false")]
        return [(
            # One line per file: size, digest, name. Computed here rather
            # than trusted from the uploader, which is the whole point of
            # showing it before anything is restored.
            "Inspect the upload",
            f"cd {grant._directory()} 2>/dev/null || exit 0;"
            " for f in *; do [ -f \"$f\" ] || continue;"
            " printf '%s %s %s\\n' \"$(stat -c %s \"$f\")\""
            " \"$(sha256sum \"$f\" | cut -d' ' -f1)\" \"$f\"; done",
        )]

    async def on_success(self, results):
        """Record the newest file found, or say the directory is empty."""
        grant = self._grant()
        rows = [
            line.strip()
            for line in results.get("Inspect the upload", {})
            .get("stdout", "").splitlines()
            if line.strip()
        ]
        if not rows:
            self._sys("No file has arrived yet.")
            return
        size, digest, name = rows[-1].split(" ", 2)
        grant.write({
            "received_filename": name,
            "received_bytes": int(size),
            "received_sha256": digest,
        })
        self._sys(f"✓ Received {name} — {size} bytes, SHA-256 {digest}")


class RevokeRestoreUploadKeyExecutor(AbstractSSHExecutor):
    """Remove the grant's key line and its directory from the host."""

    _job_type = "revoke_restore_upload_key"

    def _grant(self):
        return self.env["cloud.restore.upload.grant"].browse(
            self.job.payload.get("grant_id"),
        )

    def get_commands(self):
        grant = self._grant()
        if not grant.exists():
            return [("Nothing to revoke", "true")]
        comment = grant._key_comment()
        if not re.fullmatch(r"ic-restore-[0-9a-f]{16}", comment):
            return [("Refusing to edit keys", "false")]
        commands = [(
            # Keyed on the grant's own marker, so an operator's own keys
            # in the same file are untouched.
            "Remove the one-use key",
            f"sed -i '/{comment}/d' {authorized_keys_path()} || true",
        )]
        # A restore consuming the archive revokes the key first and reads
        # the file afterwards, so it asks for the directory to stay. The
        # sweep and the operator do not.
        if not (self.job.payload or {}).get("keep_directory"):
            commands.append((
                "Remove the upload directory",
                f"rm -rf {grant._directory()} || true",
            ))
        return commands

    async def on_success(self, results):
        self._sys("✓ Upload key revoked and directory removed.")
