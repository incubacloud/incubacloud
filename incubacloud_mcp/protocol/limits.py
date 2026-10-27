"""How much one answer may carry, and how the agent asks for the rest.

Logs, command output, mail and lists are cut at 64 KB with
``truncated: true`` and a ``next_offset`` to continue from.
"""

import json

MAX_BYTES = 64 * 1024


def _cut(text, max_bytes):
    """Return the longest prefix of *text* that fits *max_bytes* in UTF-8."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def page_items(items, offset=0, limit=None, max_bytes=MAX_BYTES):
    """Return the page of *items* starting at *offset* that fits *max_bytes*.

    A single string longer than the cap is cut so the page still fits.

    :param list items: lines or JSON-serialisable entries
    :param int offset: first index to return
    :param limit: at most this many items, or None
    :return: ``{items, offset, total, truncated, next_offset}``
    """
    offset = max(0, int(offset or 0))
    total = len(items)
    end = total if not limit else min(total, offset + max(1, int(limit)))
    page, size = [], 2
    for item in items[offset:end]:
        if isinstance(item, str):
            item = _cut(item, max_bytes - 2)
        cost = len(json.dumps(item, default=str).encode("utf-8")) + 1
        if page and size + cost > max_bytes:
            break
        page.append(item)
        size += cost
    following = offset + len(page)
    return {
        "items": page,
        "offset": offset,
        "total": total,
        "truncated": following < total,
        "next_offset": following if following < total else None,
    }


def cap_text(text, offset=0, max_bytes=MAX_BYTES):
    """Return the slice of *text* from character *offset* that fits.

    :return: ``{text, offset, total, truncated, next_offset}``
    """
    text = text or ""
    offset = max(0, int(offset or 0))
    chunk = _cut(text[offset:], max_bytes)
    following = offset + len(chunk)
    return {
        "text": chunk,
        "offset": offset,
        "total": len(text),
        "truncated": following < len(text),
        "next_offset": following if following < len(text) else None,
    }
