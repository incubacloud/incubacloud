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


def cap_strings(obj, max_bytes=MAX_BYTES):
    """Cut every string inside *obj* longer than *max_bytes*.

    A string is measured as the answer serialises it, without the two
    quotes around it, and cut to its longest prefix that fits.

    :return: ``(obj with long strings cut, whether any was cut)``
    """
    if isinstance(obj, str):
        cut = _fit(obj, max_bytes + 2)
        return cut, cut != obj
    if isinstance(obj, dict):
        pairs = {key: cap_strings(item, max_bytes) for key, item in obj.items()}
        return (
            {key: pair[0] for key, pair in pairs.items()},
            any(pair[1] for pair in pairs.values()),
        )
    if isinstance(obj, (list, tuple)):
        pairs = [cap_strings(item, max_bytes) for item in obj]
        return [pair[0] for pair in pairs], any(pair[1] for pair in pairs)
    return obj, False


#: Bytes a string keeps, in a record whose long texts are paged.
_FIELD_BYTES = 1024
#: Room left for the keys and quotes around the paged texts.
_PAGE_OVERHEAD = 256


def page_record(record, fields, offset=0, max_bytes=MAX_BYTES):
    """Return *record* within *max_bytes*, its long texts *fields* in pages.

    The texts of *fields* are read as one, in that order, from character
    *offset*: each comes back with the part of it the page holds, and
    ``next_offset`` says where the next page starts. Everything else
    comes back on every page, each string cut to 1 KB and each list to
    an eighth of the page.

    :param dict record: one entry, such as a captured message
    :param fields: the keys of its long texts
    :param int offset: where in those texts the page starts
    :return: the record with ``offset``, ``total``, ``truncated`` and
        ``next_offset``
    """
    rest, cut = cap_strings(
        {key: value for key, value in record.items() if key not in fields},
        _FIELD_BYTES,
    )
    for key, value in rest.items():
        if isinstance(value, list):
            page = page_items(value, 0, None, max_bytes // 8)
            if page["truncated"]:
                rest[key], cut = page["items"], True
    texts = [str(record.get(name) or "") for name in fields]
    room = max(0, max_bytes - _size(rest) - _PAGE_OVERHEAD)
    chunk = cap_text("".join(texts), offset, room)
    start = chunk["offset"]
    end = start + len(chunk["text"])
    position = 0
    for name, text in zip(fields, texts, strict=True):
        rest[name] = text[max(0, start - position) : max(0, end - position)]
        position += len(text)
    return {
        **rest,
        "offset": start,
        "total": chunk["total"],
        "truncated": cut or chunk["truncated"],
        "next_offset": chunk["next_offset"],
    }
