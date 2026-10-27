"""How much one answer may carry, and how the agent asks for the rest.

Logs, command output, mail and lists are cut at 64 KB with
``truncated: true`` and a ``next_offset`` to continue from. Sizes are
measured as the answer serialises them (JSON, ``ensure_ascii=False``,
UTF-8): escaped quotes and newlines count, and so does every byte of an
accented letter, no more.
"""

import json

MAX_BYTES = 64 * 1024


def _dumps(value):
    """Return *value* as a tool's answer serialises it."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _size(value):
    """Return the bytes *value* takes in a tool's answer.

    A lone surrogate is counted, not fatal: the answer escapes it.
    """
    return len(_dumps(value).encode("utf-8", errors="surrogatepass"))


def _fit(text, max_bytes):
    """Return the longest prefix of *text* that fits *max_bytes* serialised.

    Never splits a character: the prefix is counted in characters.
    """
    if _size(text) <= max_bytes:
        return text
    # Every character takes at least a byte: no longer prefix can fit.
    low, high = 0, min(len(text), max_bytes)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(text[:middle]) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def page_items(items, offset=0, limit=None, max_bytes=MAX_BYTES):
    """Return the page of *items* starting at *offset* that fits *max_bytes*.

    A first entry larger than the page is cut so the page still fits —
    a string to its longest prefix, anything else to the start of its
    JSON text — and the page says ``truncated``.

    :param list items: lines or JSON-serialisable entries
    :param int offset: first index to return
    :param limit: at most this many items, or None
    :return: ``{items, offset, total, truncated, next_offset}``
    """
    offset = max(0, int(offset or 0))
    total = len(items)
    end = total if not limit else min(total, offset + max(1, int(limit)))
    page, size, cut = [], 2, False
    for item in items[offset:end]:
        cost = _size(item) + 1
        if size + cost > max_bytes:
            if page:
                break
            text = item if isinstance(item, str) else _dumps(item)
            item = _fit(text, max_bytes - size - 1)
            cost = _size(item) + 1
            cut = True
        page.append(item)
        size += cost
    following = offset + len(page)
    return {
        "items": page,
        "offset": offset,
        "total": total,
        "truncated": cut or following < total,
        "next_offset": following if following < total else None,
    }


def cap_text(text, offset=0, max_bytes=MAX_BYTES):
    """Return the slice of *text* from character *offset* that fits.

    :return: ``{text, offset, total, truncated, next_offset}``
    """
    text = text or ""
    offset = max(0, int(offset or 0))
    chunk = _fit(text[offset:], max_bytes)
    following = offset + len(chunk)
    return {
        "text": chunk,
        "offset": offset,
        "total": len(text),
        "truncated": following < len(text),
        "next_offset": following if following < len(text) else None,
    }
