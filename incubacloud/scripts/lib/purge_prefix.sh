#!/usr/bin/env bash
# The program that empties one duplicity prefix, shared by both purges.
#
# Two callers need the exact same deletion, from two different kinds of
# container:
#
#   backup_purge.sh           — the instance's own ``backup`` service,
#                               reached with ``docker compose run``
#                               while the instance still exists.
#   backup_purge_archived.sh  — an ephemeral ``docker run`` on the host,
#                               for an archived instance whose stack was
#                               torn down when it was archived.
#
# They differ only in how the container is started; what runs inside it
# has to be identical, because the invariant they both serve is the same
# one — never leave objects belonging to an instance that no longer
# exists. Two copies of this would be two chances to fix a bug in one
# and not the other, and the drift would only show up as objects
# surviving a deletion, which is exactly what nobody notices.
#
# The program reads its target from ``DST`` in the container's
# environment, so the caller is responsible for putting it there and
# nothing about the instance is interpolated into the source.
#
# ``PURGE_BEFORE`` (optional, ISO-8601 UTC) narrows the deletion to
# objects stored before that instant. It exists because a prefix is
# derived from the instance name, so a *new* instance taking the same
# name inherits the same prefix. Deleting an old chain while its
# successor is already backing up there would take the live instance's
# objects with it — and the dangerous case is not the happy path but the
# retry: a purge that failed today and runs again next month would find
# a live instance in the prefix and empty it in silence. Anchoring the
# purge to the instant the deletion was decided makes it safe to run at
# any later time, because everything the successor writes is newer than
# the anchor. Callers with nobody alive in the prefix (the deletion of a
# live instance, which tears the instance down first) leave it unset and
# get the unconditional purge.
#
# An ``sftp://`` DST is emptied over SFTP with paramiko (in both images),
# with the same contract: the key and the trusted server key are read from
# ``/root/.ssh`` (``id_ed25519``, ``known_hosts``), any other server key is
# refused, and links are removed, never followed. With ``PURGE_BEFORE`` the
# server's clock decides what is in scope (a file's mtime), so it is
# checked first: a probe file's mtime more than 120 s off the container's
# clock stops the purge with 22. Late, it would take the successor's
# files; early, it would leave the old chain behind and answer 10.
#
# Exit codes are the contract with the executors (see backup_purge.sh):
#   0   purged and verified empty (of objects in scope)
#   10  nothing in scope was stored under the prefix
#   21  the provider rejected the credentials
#   22  anything else

# Print the deletion program on stdout, ready to pipe into a container's
# python3. Written as a quoted heredoc so nothing here is expanded by
# the host's shell on the way through.
ic_purge_prefix_program() {
    cat <<'PY'
"""Delete every object under this instance's duplicity prefix.

Runs inside a container that already has duplicity's environment, so the
credentials, endpoint and destination are read from it rather than
passed in. Prints IC_PURGE_<verdict> on the last line for the log; the
exit code is what the executor reads.

With ``PURGE_BEFORE`` set, only objects stored before that instant are
in scope; anything newer belongs to whoever holds the prefix now and is
left alone.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

dst = os.environ.get("DST", "")
if not dst:
    print("ERROR DST is empty; nothing identifies this instance's prefix")
    sys.exit(22)

cutoff = None
raw_cutoff = os.environ.get("PURGE_BEFORE", "").strip()
if raw_cutoff:
    try:
        cutoff = datetime.fromisoformat(raw_cutoff)
    except ValueError:
        # Refused rather than ignored: falling back to an unconditional
        # purge is precisely the outcome the cutoff exists to prevent.
        print(f"ERROR PURGE_BEFORE is not an ISO-8601 instant: {raw_cutoff}")
        sys.exit(22)
    if cutoff.tzinfo is None:
        # Odoo stores datetimes naive in UTC; S3 hands back aware ones.
        cutoff = cutoff.replace(tzinfo=timezone.utc)

scope = f" stored before {cutoff.isoformat()}" if cutoff else ""

# A copy of incubacloud/models/sftp_dst.py's parse(): this runs in the
# backup container, away from the panel's code. Both are tested against
# tests/data/sftp_dst_cases.json.
_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def sftp_parse(url):
    """Return ``(user, host, port, path)`` of an sftp:// DST, or raise ValueError."""
    parsed = urlparse(url)
    if parsed.scheme != "sftp" or parsed.password is not None:
        raise ValueError("not an sftp:// destination")
    user, host = parsed.username or "", parsed.hostname or ""
    if not user or not host or not _USER_RE.match(user):
        raise ValueError("no user or no server")
    port = parsed.port or 22
    raw = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
    raw = raw.strip()
    absolute = raw.startswith("/")
    body = raw.strip("/")
    if not body:
        if absolute:
            raise ValueError("the root directory")
        return user, host, port, ""
    for segment in body.split("/"):
        if not segment or segment in (".", "..") or not _SEGMENT_RE.match(segment):
            raise ValueError(f"path segment not allowed: {segment!r}")
    return user, host, port, ("/" if absolute else "") + body


def sftp_purge():
    """Empty an sftp:// prefix; exits with the contract's code."""
    try:
        user, host, port, path = sftp_parse(dst)
    except ValueError as exc:
        print(f"ERROR not a usable sftp:// destination ({exc}): {dst}")
        sys.exit(22)
    if len([s for s in path.split("/") if s]) < 2:
        # Same safety as the bare bucket: {project}/{instance} at least.
        print(f"ERROR refusing to purge without a per-instance prefix: {dst}")
        sys.exit(22)
    try:
        import paramiko
    except ImportError:
        print("ERROR paramiko unavailable inside the backup container")
        sys.exit(22)
    import errno
    import secrets
    import stat as st

    ssh_dir = os.path.expanduser("~/.ssh")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.load_host_keys(os.path.join(ssh_dir, "known_hosts"))
    except OSError as exc:
        print(f"ERROR no trusted server key to check {host} against: {exc}")
        sys.exit(22)
    try:
        client.connect(
            host, port=port, username=user,
            key_filename=os.path.join(ssh_dir, "id_ed25519"),
            look_for_keys=False, allow_agent=False,
            timeout=30, banner_timeout=30, auth_timeout=30,
        )
    except paramiko.BadHostKeyException:
        print(f"ERROR the server key of {host} does not match the trusted one")
        sys.exit(22)
    except paramiko.AuthenticationException:
        print(f"ERROR {host} rejected the key")
        sys.exit(21)
    except (paramiko.SSHException, OSError) as exc:
        print(f"ERROR cannot reach {host}:{port}: {exc}")
        sys.exit(22)
    sftp = client.open_sftp()

    def is_dir(attrs):
        return attrs.st_mode is not None and st.S_ISDIR(attrs.st_mode)

    def walk():
        """Return ``(files, dirs)`` under path; links count as files."""
        files, dirs, pending = [], [], [path]
        while pending:
            current = pending.pop()
            dirs.append(current)
            for attrs in sftp.listdir_attr(current):
                full = f"{current}/{attrs.filename}"
                if is_dir(attrs):
                    pending.append(full)
                else:
                    files.append((full, attrs.st_mtime or 0))
        return files, dirs

    def in_scope(mtime):
        return cutoff is None or mtime < cutoff.timestamp()

    try:
        try:
            sftp.lstat(path)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                print(f"IC_PURGE_EMPTY nothing{scope} was stored under {path}")
                sys.exit(10)
            raise
        if cutoff is not None:
            probe = f"{path}/.incubacloud-clock-{secrets.token_hex(6)}"
            with sftp.open(probe, "w") as fh:
                fh.write("clock\n")
            try:
                skew = sftp.stat(probe).st_mtime - time.time()
            finally:
                sftp.remove(probe)
            if abs(skew) > 120:
                print(
                    f"ERROR the clock of {host} is {skew:+.0f}s off this one: "
                    "PURGE_BEFORE cannot be trusted there"
                )
                sys.exit(22)
        files, dirs = walk()
        deleted = 0
        for name, mtime in files:
            if in_scope(mtime):
                sftp.remove(name)
                deleted += 1
        # Empty folders only, deepest first; one still holding a file out
        # of scope (the successor's) refuses, which is expected.
        for folder in sorted(dirs, key=lambda d: d.count("/"), reverse=True):
            try:
                sftp.rmdir(folder)
            except OSError:
                pass
        try:
            remaining = sum(1 for _n, m in walk()[0] if in_scope(m))
        except OSError as exc:
            if getattr(exc, "errno", None) != errno.ENOENT:
                raise
            remaining = 0
    except OSError as exc:
        print(f"ERROR SFTP error while purging {path}: {exc}")
        sys.exit(22)
    finally:
        client.close()
    if remaining:
        print(f"ERROR prefix still has files{scope} after purge: {path}")
        sys.exit(22)
    if deleted == 0:
        print(f"IC_PURGE_EMPTY nothing{scope} was stored under {path}")
        sys.exit(10)
    print(f"IC_PURGE_OK removed {deleted} file(s){scope} under {path}")
    sys.exit(0)


if dst.startswith("sftp://"):
    sftp_purge()

try:
    import boto3
    from botocore.exceptions import ClientError, EndpointConnectionError
except ImportError:
    print("ERROR boto3 unavailable inside the backup container")
    sys.exit(22)

# DST looks like boto3+s3://bucket/path/project/instance
parsed = urlparse(dst.replace("boto3+s3://", "s3://"))
bucket = parsed.netloc
prefix = parsed.path.strip("/")
if not bucket or not prefix:
    # Refusing a bare bucket is the whole safety story: a prefix-less
    # delete would take every instance sharing this destination.
    print(f"ERROR refusing to purge without a per-instance prefix: {dst}")
    sys.exit(22)
prefix += "/"


def in_scope(obj):
    """Whether this object predates the cutoff (always true without one).

    :param obj: one ``Contents`` entry from ``list_objects_v2``.
    :return: True when the object may be deleted by this purge.
    """
    return cutoff is None or obj["LastModified"] < cutoff


kwargs = {}
endpoint = os.environ.get("AWS_ENDPOINT_URL")
if endpoint:
    kwargs["endpoint_url"] = endpoint

client = boto3.client("s3", **kwargs)
deleted = 0
try:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        batch = [
            {"Key": o["Key"]}
            for o in page.get("Contents") or ()
            if in_scope(o)
        ]
        if not batch:
            continue
        client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
        deleted += len(batch)

    # Verify rather than trust: a delete_objects that silently reports
    # per-key errors would otherwise look like success. Walked with the
    # same filter rather than counted, because with a cutoff the objects
    # left behind are the successor's and finding them is the expected
    # outcome, not a failed deletion.
    remaining = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        remaining = sum(1 for o in page.get("Contents") or () if in_scope(o))
        if remaining:
            break
except ClientError as exc:
    code = (exc.response.get("Error") or {}).get("Code", "")
    if code in ("AccessDenied", "InvalidAccessKeyId",
                "SignatureDoesNotMatch", "ExpiredToken", "403"):
        print(f"ERROR provider rejected the credentials: {code}")
        sys.exit(21)
    if code in ("NoSuchBucket", "404"):
        print(f"IC_PURGE_EMPTY bucket is gone: {bucket}")
        sys.exit(10)
    print(f"ERROR S3 error while purging {prefix}: {code}")
    sys.exit(22)
except EndpointConnectionError as exc:
    print(f"ERROR cannot reach the storage endpoint: {exc}")
    sys.exit(22)

if remaining:
    print(f"ERROR prefix still has objects{scope} after purge: {prefix}")
    sys.exit(22)
if deleted == 0:
    print(f"IC_PURGE_EMPTY nothing{scope} was stored under {prefix}")
    sys.exit(10)
print(f"IC_PURGE_OK removed {deleted} object(s){scope} under {prefix}")
PY
}
