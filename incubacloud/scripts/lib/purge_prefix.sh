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
# Exit codes are the contract with the executors (see backup_purge.sh):
#   0   purged and verified empty
#   10  the prefix was already empty
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
"""
import os
import sys
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

kwargs = {}
endpoint = os.environ.get("AWS_ENDPOINT_URL")
if endpoint:
    kwargs["endpoint_url"] = endpoint

client = boto3.client("s3", **kwargs)
deleted = 0
try:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        batch = [{"Key": o["Key"]} for o in page.get("Contents") or ()]
        if not batch:
            continue
        client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
        deleted += len(batch)

    # Verify rather than trust: a delete_objects that silently reports
    # per-key errors would otherwise look like success.
    remaining = client.list_objects_v2(
        Bucket=bucket, Prefix=prefix, MaxKeys=1,
    ).get("KeyCount", 0)
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
    print(f"ERROR prefix still has objects after purge: {prefix}")
    sys.exit(22)
if deleted == 0:
    print(f"IC_PURGE_EMPTY nothing was stored under {prefix}")
    sys.exit(10)
print(f"IC_PURGE_OK removed {deleted} object(s) under {prefix}")
PY
}
