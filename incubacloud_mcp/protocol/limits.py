"""How much one answer may carry, and how the agent asks for the rest.

Logs, command output, mail and lists are cut at 64 KB with
``truncated: true`` and a ``next_offset`` to continue from. Sizes are
measured as the answer serialises them (JSON, ``ensure_ascii=False``,
UTF-8): escaped quotes and newlines count, and so does every byte of an
accented letter, no more.
"""

import copy
import json

MAX_BYTES = 64 * 1024
#: Bytes between two entries of a list, as the answer serialises it: ``, ``.
SEPARATOR_BYTES = 2
#: The keys under which a paged answer carries its page of entries.
PAGE_LIST_KEYS = ("items", "entries", "alerts")


def _dumps(value):
    """Return *value* as a tool's answer serialises it."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _size(value):
    """Return the bytes *value* takes in a tool's answer.

    A lone surrogate is counted, not fatal: the answer escapes it.
    """
    return len(_dumps(value).encode("utf-8", errors="surrogatepass"))


def size_of(value):
    """Return the bytes *value* takes in a tool's answer."""
    return _size(value)


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
        cost = _size(item) + SEPARATOR_BYTES
        if size + cost > max_bytes:
            if page:
                break
            text = item if isinstance(item, str) else _dumps(item)
            item = _fit(text, max_bytes - size - SEPARATOR_BYTES)
            cost = _size(item) + SEPARATOR_BYTES
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


def page_answer(
    answer, key, offset=0, limit=None, first=0, total=None, max_bytes=MAX_BYTES
):
    """Return a route's *answer* with its list *key* cut to a page that fits.

    The list holds the entries from index *first* on: ``0`` when the
    route answered them all and the tool pages them, *offset* when the
    route paged them itself. The rest of the answer comes on every page.

    :param dict answer: what the route answered
    :param str key: the key of its long list
    :param int offset: where the page starts, among all the entries
    :param limit: at most this many entries, or None
    :param int first: the index of the list's first entry
    :param total: how many entries there are, if the route says so
    :return: the answer with ``offset``, ``truncated`` and ``next_offset``
    """
    rest = {name: value for name, value in answer.items() if name != key}
    items = answer.get(key) or []
    offset = max(0, int(offset or 0))
    room = max(0, max_bytes - _size(rest) - _PAGE_OVERHEAD)
    page = page_items(items, offset - first, limit, room)
    following = offset + len(page["items"])
    known = total if isinstance(total, int) else first + len(items)
    more = following < known
    return {
        **rest,
        key: page["items"],
        "offset": offset,
        "truncated": page["truncated"] or more,
        "next_offset": following if more else None,
    }


def _head_lists(obj, count):
    """Return *obj* with every list in it cut to its first *count* entries."""
    if isinstance(obj, dict):
        return {key: _head_lists(item, count) for key, item in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_head_lists(item, count) for item in obj[:count]]
    return obj


def _longest_list(obj):
    """Return the length of the longest list inside *obj*."""
    if isinstance(obj, dict):
        return max((_longest_list(item) for item in obj.values()), default=0)
    if isinstance(obj, (list, tuple)):
        return max([len(obj), *(_longest_list(item) for item in obj)])
    return 0


def _page_lists(obj):
    """Return ``[(page, key)]``: the pages of entries inside *obj*.

    A page is a dict with an integer ``offset``, a ``next_offset`` and its
    entries in a list under one of :data:`PAGE_LIST_KEYS`. Walked without
    recursion.
    """
    found, stack = [], [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("offset"), int) and "next_offset" in node:
                key = next(
                    (
                        name
                        for name in PAGE_LIST_KEYS
                        if isinstance(node.get(name), list)
                    ),
                    None,
                )
                if key:
                    found.append((node, key))
                    continue
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return found


def _has_next_offset(obj):
    """Return whether a ``next_offset`` key is anywhere inside *obj*."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "next_offset" in node:
                return True
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False


def _fit_page(obj, max_bytes):
    """Cut the page of entries of *obj* until *obj* fits, moving ``next_offset``.

    The entries left out are those after the last one kept: the next page
    starts at the first of them, so none is skipped. When not even one
    entry fits, the first is cut, as :func:`page_items` does.

    :param obj: a copy of the answer, changed in place
    :return: whether it now fits
    """
    page, key = max(_page_lists(obj), key=lambda found: len(found[0][found[1]]))
    entries = page[key]

    def fits(count):
        """Keep the first *count* entries; return whether *obj* fits."""
        page[key] = entries[:count]
        return _size(obj) <= max_bytes

    if not fits(0):
        page[key] = entries
        return False
    low, high = 0, len(entries)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    kept = entries[:low]
    if not kept and entries:
        page[key] = []
        first = entries[0]
        text = first if isinstance(first, str) else _dumps(first)
        cut = _fit(text, max_bytes - _size(obj) - SEPARATOR_BYTES - 2)
        kept = [cut] if cut else []
    page[key] = kept
    if len(kept) < len(entries):
        page["next_offset"] = page["offset"] + len(kept)
        page["truncated"] = True
    return True


def fit_answer(obj, max_bytes=MAX_BYTES):
    """Return ``(obj, False)`` when *obj* fits *max_bytes*, else a cut of it.

    The safety net behind the tools that page their answers, which
    masking can still grow past the page. A page of entries loses its
    last entries and its ``next_offset`` moves back to the first of them:
    nothing is skipped. A page of text, whose offsets count the
    characters before masking, is left as it is. Any other answer has its
    lists cut first, every one to the same number of entries, the most
    that fits; when even empty lists do not make it fit, strings are cut
    to 1 KB first.

    :return: ``(answer that fits, whether it was cut)``
    """
    if _size(obj) <= max_bytes:
        return obj, False
    if _page_lists(obj):
        paged = copy.deepcopy(obj)
        if _fit_page(paged, max_bytes):
            return paged, True
        return obj, False
    if _has_next_offset(obj):
        return obj, False
    for candidate in (obj, cap_strings(obj, _FIELD_BYTES)[0]):
        if _size(_head_lists(candidate, 0)) > max_bytes:
            continue
        low, high = 0, _longest_list(candidate)
        while low < high:
            middle = (low + high + 1) // 2
            if _size(_head_lists(candidate, middle)) <= max_bytes:
                low = middle
            else:
                high = middle - 1
        return _head_lists(candidate, low), True
    return {"truncated": True, "error": "The answer is too large to show."}, True
