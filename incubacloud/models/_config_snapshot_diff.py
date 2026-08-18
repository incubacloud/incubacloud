"""Comparison helpers for the config-drift snapshot.

``applied_config_hash`` answers *whether* the saved configuration still
matches what the last deploy shipped, and nothing else. When the whole
fleet lit up its "Changes not deployed" pill on 2026-08-18 — a release
had added one copier answer, which moves every instance's hash at once —
the only way to find out *what* had moved was to rebuild the snapshot by
hand and test hypotheses against it. Storing the snapshot alongside its
hash turns that into a lookup.

Shared by ``cloud.instance`` and ``cloud.host``, which keep separate
(and differently shaped) snapshots.
"""
import json


def normalize(snapshot):
    """Return ``snapshot`` as the plain JSON the hash is computed over.

    The stored copy has to survive a round-trip through a ``Json``
    column, and the comparison has to be against the same shape the hash
    saw — otherwise a tuple-vs-list or a stringified value would read as
    drift that no rebuild can ever clear.
    """
    return json.loads(json.dumps(snapshot, sort_keys=True, default=str))


def _flatten(value, prefix=""):
    """Yield ``(dotted_key, scalar)`` pairs for a nested snapshot."""
    if isinstance(value, dict):
        for key, sub in value.items():
            yield from _flatten(sub, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield from _flatten(sub, f"{prefix}[{index}]")
    else:
        yield prefix, value


def diff_keys(stored, current):
    """Return the sorted dotted keys whose value differs.

    A key present on one side only counts as a difference — that is
    exactly the shape of the incident this exists for, where a release
    introduced a new answer.
    """
    old = dict(_flatten(normalize(stored)))
    new = dict(_flatten(normalize(current)))
    missing = object()
    return sorted(
        key for key in set(old) | set(new)
        if old.get(key, missing) != new.get(key, missing)
    )
