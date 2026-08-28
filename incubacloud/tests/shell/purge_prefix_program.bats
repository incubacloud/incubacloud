#!/usr/bin/env bats
# Tests for the deletion program emitted by scripts/lib/purge_prefix.sh.
#
# backup_purge.bats stubs ``docker`` and therefore only ever greps this
# program as text. What it cannot check is the part that decides which
# objects die, and that part now has a cutoff: a purge is bounded to the
# chain that existed when the deletion was decided, because a prefix is
# derived from the instance name and a new instance can inherit it.
#
# So here the program is actually run, against a fake ``boto3`` backed
# by a JSON file. Stubbing the SDK rather than the shell is what makes
# "the successor's objects survived" an assertion instead of a hope.

setup() {
    LIB="${BATS_TEST_DIRNAME}/../../scripts/lib/purge_prefix.sh"
    TMP="$(mktemp -d)"
    export FAKE_S3_STATE="$TMP/state.json"
    export DST="boto3+s3://bucket/backups/proj/inst"

    mkdir -p "$TMP/pylib/botocore"
    cat > "$TMP/pylib/boto3.py" <<'FAKE'
"""Minimal S3 stand-in: the object store is a JSON file of key -> ISO date."""
import datetime
import json
import os

_STATE = os.environ["FAKE_S3_STATE"]


class _Paginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix):
        objs = self.client._objects(Prefix)
        yield {"Contents": objs} if objs else {}


class _Client:
    def _load(self):
        with open(_STATE) as fh:
            return json.load(fh)

    def _objects(self, prefix):
        return [
            {"Key": k, "LastModified": datetime.datetime.fromisoformat(v)}
            for k, v in sorted(self._load().items())
            if k.startswith(prefix)
        ]

    def get_paginator(self, _name):
        return _Paginator(self)

    def delete_objects(self, Bucket, Delete):
        data = self._load()
        for obj in Delete["Objects"]:
            data.pop(obj["Key"], None)
        with open(_STATE, "w") as fh:
            json.dump(data, fh)
        return {}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=None):
        objs = self._objects(Prefix)
        return {"KeyCount": len(objs), "Contents": objs}


def client(_name, **_kwargs):
    return _Client()
FAKE

    cat > "$TMP/pylib/botocore/__init__.py" <<'FAKE'
FAKE
    cat > "$TMP/pylib/botocore/exceptions.py" <<'FAKE'
class ClientError(Exception):
    def __init__(self, response=None, operation_name=None):
        super().__init__("client error")
        self.response = response or {}


class EndpointConnectionError(Exception):
    pass
FAKE

    export PYTHONPATH="$TMP/pylib"
}

teardown() {
    rm -rf "$TMP"
}

# Seed the fake bucket. Args are ``key=iso8601`` pairs.
_seed() {
    python3 - "$@" <<'PY'
import json
import os
import sys

data = {}
for arg in sys.argv[1:]:
    key, _, when = arg.partition("=")
    data[key] = when
with open(os.environ["FAKE_S3_STATE"], "w") as fh:
    json.dump(data, fh)
PY
}

# Keys still in the fake bucket, one per line.
_remaining() {
    python3 - <<'PY'
import json
import os

with open(os.environ["FAKE_S3_STATE"]) as fh:
    for key in sorted(json.load(fh)):
        print(key)
PY
}

_run_program() {
    # shellcheck source=../../scripts/lib/purge_prefix.sh
    source "$LIB"
    ic_purge_prefix_program > "$TMP/prog.py"
    python3 "$TMP/prog.py"
}

@test "without a cutoff every object under the prefix goes" {
    _seed \
        "backups/proj/inst/one=2026-01-01T00:00:00+00:00" \
        "backups/proj/inst/two=2026-06-01T00:00:00+00:00" \
        "backups/other/keep=2026-01-01T00:00:00+00:00"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed 2 object(s)"* ]]
    # A sibling instance sharing the destination is untouched.
    [ "$(_remaining)" = "backups/other/keep" ]
}

@test "with a cutoff only what predates it is deleted" {
    _seed \
        "backups/proj/inst/old-full=2026-01-01T00:00:00+00:00" \
        "backups/proj/inst/old-inc=2026-01-02T00:00:00+00:00" \
        "backups/proj/inst/successor=2026-03-01T00:00:00+00:00"
    export PURGE_BEFORE="2026-02-01T00:00:00+00:00"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed 2 object(s)"* ]]
    [[ "$output" == *"stored before"* ]]
    # The whole point: the instance that holds the prefix now kept its
    # backups.
    [ "$(_remaining)" = "backups/proj/inst/successor" ]
}

@test "a retry after the successor arrived is a no-op, not a wipe" {
    # The failure mode the cutoff exists for. The first purge dies, the
    # customer starts from scratch, the new instance backs up under the
    # same prefix, and the retry lands. Without the cutoff this deletes
    # the live instance's chain and reports success.
    _seed "backups/proj/inst/successor=2026-03-01T00:00:00+00:00"
    export PURGE_BEFORE="2026-02-01T00:00:00+00:00"
    run _run_program
    [ "$status" -eq 10 ]
    [ "$(_remaining)" = "backups/proj/inst/successor" ]
}

@test "a cutoff that predates the whole chain deletes nothing" {
    _seed "backups/proj/inst/full=2026-05-01T00:00:00+00:00"
    export PURGE_BEFORE="2026-01-01T00:00:00+00:00"
    run _run_program
    [ "$status" -eq 10 ]
    [[ "$output" == *"nothing"* ]]
    [ "$(_remaining)" = "backups/proj/inst/full" ]
}

@test "a naive cutoff is read as UTC" {
    # Odoo stores datetimes naive in UTC, so this is the shape the
    # executor actually sends.
    _seed \
        "backups/proj/inst/old=2026-01-01T00:00:00+00:00" \
        "backups/proj/inst/new=2026-03-01T00:00:00+00:00"
    export PURGE_BEFORE="2026-02-01T00:00:00"
    run _run_program
    [ "$status" -eq 0 ]
    [ "$(_remaining)" = "backups/proj/inst/new" ]
}

@test "an unparseable cutoff refuses instead of purging everything" {
    # Falling back to the unconditional purge is the exact outcome the
    # cutoff exists to prevent, so a bad value must never mean "ignore".
    _seed "backups/proj/inst/full=2026-01-01T00:00:00+00:00"
    export PURGE_BEFORE="last tuesday"
    run _run_program
    [ "$status" -eq 22 ]
    [[ "$output" == *"not an ISO-8601 instant"* ]]
    [ "$(_remaining)" = "backups/proj/inst/full" ]
}

@test "an empty cutoff is the same as no cutoff" {
    _seed "backups/proj/inst/full=2026-01-01T00:00:00+00:00"
    export PURGE_BEFORE=""
    run _run_program
    [ "$status" -eq 0 ]
    [ -z "$(_remaining)" ]
}

@test "a bare bucket is still refused with a cutoff set" {
    _seed "anything=2026-01-01T00:00:00+00:00"
    export DST="boto3+s3://bucket"
    export PURGE_BEFORE="2026-02-01T00:00:00+00:00"
    run _run_program
    [ "$status" -eq 22 ]
    [[ "$output" == *"refusing to purge without a per-instance prefix"* ]]
}
