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
# ``PURGE_SINCE`` (optional, ISO-8601 UTC) is the other side: only
# objects stored at or after that instant are in scope. The purges of
# that successor (its deletion, the purge of its archived copy, of its
# recovery archive switched off) set it to the instant the successor was
# created, so what the archived instance of the same name keeps in the
# prefix, stored before, stays for that instance's own purge.
#
# With ``PURGE_BEFORE``, an object it keeps may still need one it would
# delete: duplicity names an incremental set, and its signatures, after
# the instant the set it builds on ends (``duplicity-inc.<T1>.to.<T2>``,
# ``duplicity-new-signatures.<T1>.to.<T2>``). An instance that took the
# name of an archived one and inherited its path continues that one's
# chain with its first backup, and deleting the archived full leaves
# every set after it unrestorable. So when an object kept for being
# stored at or after the cutoff names a start before it (and, with
# ``PURGE_SINCE``, not before that either: those stay), nothing is
# deleted and the program exits 23. Listed whole before the first delete.
#
# An ``sftp://`` DST is emptied over SFTP with paramiko (in both images),
# with the same contract: the key and the trusted server key are read from
# ``/root/.ssh`` (``id_ed25519``, ``known_hosts``), any other server key is
# refused, and links are removed, never followed. A destination that logs
# in with a password has no key there: ``BACKEND_PASSWORD``, which
# duplicity logs in with, is in the environment, and the purge uses it.
# With ``PURGE_BEFORE`` or ``PURGE_SINCE`` the server's clock decides what
# is in scope (a file's mtime), so it is checked first: a probe file's
# mtime more than 120 s off the container's clock stops the purge with 22.
# Off, it would take the other instance's files, or leave this one's behind
# and answer 10. The chain check (23) reads a file's mtime where S3 gives
# ``LastModified``; a file the server gives no time for is kept, and counts
# as newer.
#
# Exit codes are the contract with the executors (see backup_purge.sh):
#   0   purged and verified empty (of objects in scope)
#   10  nothing in scope was stored under the prefix
#   21  the provider rejected the credentials
#   22  anything else
#   23  an object kept continues a chain the purge would delete: nothing
#       deleted (only with ``PURGE_BEFORE``)

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
left alone, and should one of those continue a duplicity chain this
purge would delete, nothing is deleted (exit 23). With ``PURGE_SINCE``
set, only objects stored at or after it are; anything older belongs to
the archived instance of the same name.
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

def read_instant(name):
    """Read the ISO-8601 instant in the environment variable *name*.

    :param str name: ``PURGE_BEFORE`` or ``PURGE_SINCE``
    :return: the instant, aware (a naive one is UTC), or None when unset
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        instant = datetime.fromisoformat(raw)
    except ValueError:
        # Refused rather than ignored: falling back to an unconditional
        # purge is precisely the outcome the cutoff exists to prevent.
        print(f"ERROR {name} is not an ISO-8601 instant: {raw}")
        sys.exit(22)
    if instant.tzinfo is None:
        # Odoo stores datetimes naive in UTC; S3 hands back aware ones.
        instant = instant.replace(tzinfo=timezone.utc)
    return instant


cutoff = read_instant("PURGE_BEFORE")
since = read_instant("PURGE_SINCE")

scope = f" stored before {cutoff.isoformat()}" if cutoff else ""
if since:
    scope += f" stored since {since.isoformat()}"

#: The start of the set a duplicity incremental, or its signatures, builds
#: on, in the object's name (after the file prefix the image adds).
SET_START = re.compile(r"duplicity-(?:inc|new-signatures)\.(\d{8}T\d{6}Z)\.to\.")


def continues_scope(name, stored):
    """Whether a file kept for being newer needs a set the purge deletes.

    Shared by both destinations: S3 passes the object's ``LastModified``,
    SFTP the file's mtime.

    :param str name: the object's key or the file's path
    :param stored: when it was stored, aware; None when the server gave
        no time, which counts as at or after the cutoff: it is kept, and
        whether it is newer cannot be told
    :return: True for a duplicity incremental or its signatures stored at
        or after the cutoff whose set starts before it (and not before
        ``PURGE_SINCE``, whose objects stay).
    """
    if cutoff is None or (stored is not None and stored < cutoff):
        return False
    found = SET_START.search(name.rsplit("/", 1)[-1])
    if not found:
        return False
    start = datetime.strptime(found.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc,
    )
    return start < cutoff and (since is None or start >= since)


def refuse_continued(depends, where):
    """Exit 23 when a kept file continues a chain the purge would delete.

    :param list depends: the names of those files, empty when none
    :param str where: the prefix or path, for the log
    """
    if depends:
        print(
            f"IC_PURGE_DEPENDS {len(depends)} object(s) stored since "
            f"{cutoff.isoformat()} continue a chain this purge would delete, "
            f"first {depends[0]}: nothing deleted under {where}"
        )
        sys.exit(23)

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
    if "?" in url or "#" in url:
        raise ValueError("a query or a fragment")
    user, host = parsed.username or "", parsed.hostname or ""
    if not user or not host or not _USER_RE.match(user):
        raise ValueError("no user or no server")
    port = parsed.port
    if port == 0:
        raise ValueError("port 0")
    port = port or 22
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
    password = os.environ.get("BACKEND_PASSWORD") or None
    if password:
        login, what = {"password": password}, "password"
    else:
        login, what = {"key_filename": os.path.join(ssh_dir, "id_ed25519")}, "key"
    try:
        client.connect(
            host, port=port, username=user, **login,
            look_for_keys=False, allow_agent=False,
            timeout=30, banner_timeout=30, auth_timeout=30,
        )
    except paramiko.BadHostKeyException:
        print(f"ERROR the server key of {host} does not match the trusted one")
        sys.exit(22)
    except paramiko.AuthenticationException:
        print(f"ERROR {host} rejected the {what}")
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
                    files.append((full, attrs.st_mtime))
        return files, dirs

    def in_scope(mtime):
        # A file the server gives no time for cannot be told older than
        # the cutoff nor newer than the start: with either, it is left
        # alone.
        if cutoff is None and since is None:
            return True
        if mtime is None:
            return False
        return (cutoff is None or mtime < cutoff.timestamp()) and (
            since is None or mtime >= since.timestamp()
        )

    try:
        try:
            sftp.lstat(path)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                print(f"IC_PURGE_EMPTY nothing{scope} was stored under {path}")
                sys.exit(10)
            raise
        if cutoff is not None or since is not None:
            # An empty file, removed whatever happens: on a full server
            # the probe needs no space, and a failed one leaves nothing.
            probe = f"{path}/.incubacloud-clock-{secrets.token_hex(6)}"
            try:
                sftp.open(probe, "w").close()
                skew = (sftp.stat(probe).st_mtime or 0) - time.time()
            finally:
                try:
                    sftp.remove(probe)
                except OSError:
                    pass
            if abs(skew) > 120:
                print(
                    f"ERROR the clock of {host} is {skew:+.0f}s off this one: "
                    "PURGE_BEFORE and PURGE_SINCE cannot be trusted there"
                )
                sys.exit(22)
        files, dirs = walk()
        # Listed whole first, as on S3: the chain check sees every file
        # kept before anything is deleted.
        refuse_continued([
            name for name, mtime in files
            if not in_scope(mtime) and continues_scope(
                name,
                None if mtime is None
                else datetime.fromtimestamp(mtime, timezone.utc),
            )
        ], path)
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
    """Whether this object lies within the bounds (always true without any).

    :param obj: one ``Contents`` entry from ``list_objects_v2``.
    :return: True when the object may be deleted by this purge.
    """
    when = obj["LastModified"]
    return (cutoff is None or when < cutoff) and (since is None or when >= since)


kwargs = {}
endpoint = os.environ.get("AWS_ENDPOINT_URL")
if endpoint:
    kwargs["endpoint_url"] = endpoint

client = boto3.client("s3", **kwargs)
deleted = 0
try:
    paginator = client.get_paginator("list_objects_v2")
    # Listed whole first: the dependency check has to see every object
    # kept before anything is deleted.
    doomed = []
    depends = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents") or ():
            if in_scope(o):
                doomed.append({"Key": o["Key"]})
            elif continues_scope(o["Key"], o["LastModified"]):
                depends.append(o["Key"])
    refuse_continued(depends, prefix)
    # delete_objects takes up to 1000 keys a call.
    for start in range(0, len(doomed), 1000):
        batch = doomed[start:start + 1000]
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
