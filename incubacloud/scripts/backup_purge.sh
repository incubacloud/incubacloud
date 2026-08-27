#!/usr/bin/env bash
# Empty an instance's backup prefix before its stack is torn down.
#
# Usage: backup_purge.sh <instance_dir>
#
# The invariant this serves: never leave objects belonging to an
# instance that no longer exists. The only thing that can talk to the
# bucket is the instance's own ``backup`` container — it holds the
# credentials, the endpoint and the destination in ``.docker/backup.env``
# — and ``compose down -v`` destroys it. So the purge has to run
# *before* the teardown, from here, and if it cannot the caller must
# abort rather than strand the objects with nothing left to delete them.
#
# ``run --rm`` rather than ``exec``: it builds the container from the
# compose definition without needing the stack up, so a stopped instance
# purges just as well as a running one.
#
# duplicity cannot do this itself. ``remove-all-but-n-full`` requires
# n>=1 and there is no command that removes the last full, so the delete
# goes through the S3 API from inside the container, reusing the very
# same environment duplicity would have used.
#
# Exit codes are the contract with the executor, which classifies the
# failure from the code alone. Matching duplicity/boto text would rot the
# moment either changes version and silently downgrade every diagnosis to
# "unknown":
#
#   0   purged — the prefix is empty and was verified empty
#   10  nothing to purge — the prefix was already empty (not a failure:
#       the invariant already holds, so the caller proceeds)
#   20  no ``backup`` service in this compose (panel/host drift)
#   21  the provider rejected the credentials
#   22  the container could not start, or any other failure
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "backup_purge.sh <instance_dir>"

dir="$(ic_expand_home "$1")"

if [ ! -d "$dir" ]; then
    # No directory means no compose and no credentials. The caller only
    # sends us here when the panel says this instance has backups, so a
    # missing directory is drift, not an empty prefix.
    ic_warn "instance directory not found: $dir"
    exit 20
fi

cd "$dir"

if ! docker compose config --services 2>/dev/null | grep -qx backup; then
    # Deliberately checked here and not inferred from a failed exec:
    # ``docker compose exec`` answers `service "backup" is not running`
    # both when the service is stopped and when it does not exist, so
    # that message can never tell the two apart.
    ic_warn "no 'backup' service declared in $dir"
    exit 20
fi

ic_log "purging backup prefix via the instance's own backup container"

set +e
docker compose run --rm -T --entrypoint python3 backup - <<'PY'
"""Delete every object under this instance's duplicity prefix.

Runs inside the backup container, so the credentials, endpoint and
destination are already in the environment that duplicity uses. Prints
IC_PURGE_<verdict> on the last line for the log; the exit code is what
the executor reads.
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
rc=$?
set -e

case "$rc" in
    0)  ic_log "backup prefix purged." ;;
    10) ic_log "backup prefix was already empty." ;;
    21) ic_warn "storage rejected the credentials." ;;
    20) ic_warn "backup service unavailable." ;;
    *)  ic_warn "backup purge failed (exit $rc)."
        # Anything the inner script did not classify — most often the
        # container failing to start at all, e.g. the 64-byte hostname
        # limit — lands here as 22.
        rc=22 ;;
esac

exit "$rc"
