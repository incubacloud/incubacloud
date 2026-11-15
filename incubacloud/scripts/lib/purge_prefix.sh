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
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import boto3
    from botocore.exceptions import ClientError, EndpointConnectionError
except ImportError:
    print("ERROR boto3 unavailable inside the backup container")
    sys.exit(22)

dst = os.environ.get("DST", "")
if not dst:
    print("ERROR DST is empty; nothing identifies this instance's prefix")
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

#: The start of the set a duplicity incremental, or its signatures, builds
#: on, in the object's name (after the file prefix the image adds).
SET_START = re.compile(r"duplicity-(?:inc|new-signatures)\.(\d{8}T\d{6}Z)\.to\.")


def in_scope(obj):
    """Whether this object lies within the bounds (always true without any).

    :param obj: one ``Contents`` entry from ``list_objects_v2``.
    :return: True when the object may be deleted by this purge.
    """
    when = obj["LastModified"]
    return (cutoff is None or when < cutoff) and (since is None or when >= since)


def builds_on_scope(obj):
    """Whether this object, kept for being newer, needs a set the purge deletes.

    :param obj: one ``Contents`` entry from ``list_objects_v2``, out of scope.
    :return: True for a duplicity incremental or its signatures stored at
        or after the cutoff whose set starts before it (and not before
        ``PURGE_SINCE``, whose objects stay).
    """
    if cutoff is None or obj["LastModified"] < cutoff:
        return False
    found = SET_START.search(obj["Key"].rsplit("/", 1)[-1])
    if not found:
        return False
    start = datetime.strptime(found.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc,
    )
    return start < cutoff and (since is None or start >= since)


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
            elif builds_on_scope(o):
                depends.append(o["Key"])
    if depends:
        print(
            f"IC_PURGE_DEPENDS {len(depends)} object(s) stored since "
            f"{cutoff.isoformat()} continue a chain this purge would delete, "
            f"first {depends[0]}: nothing deleted under {prefix}"
        )
        sys.exit(23)
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

scope = f" stored before {cutoff.isoformat()}" if cutoff else ""
if since:
    scope += f" stored since {since.isoformat()}"
if remaining:
    print(f"ERROR prefix still has objects{scope} after purge: {prefix}")
    sys.exit(22)
if deleted == 0:
    print(f"IC_PURGE_EMPTY nothing{scope} was stored under {prefix}")
    sys.exit(10)
print(f"IC_PURGE_OK removed {deleted} object(s){scope} under {prefix}")
PY
}
