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

scope = f" stored before {cutoff.isoformat()}" if cutoff else ""
if remaining:
    print(f"ERROR prefix still has objects{scope} after purge: {prefix}")
    sys.exit(22)
if deleted == 0:
    print(f"IC_PURGE_EMPTY nothing{scope} was stored under {prefix}")
    sys.exit(10)
print(f"IC_PURGE_OK removed {deleted} object(s){scope} under {prefix}")
PY
}
