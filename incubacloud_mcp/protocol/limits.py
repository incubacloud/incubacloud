"""How much one answer may carry, and how the agent asks for the rest.

Logs, mail and lists are cut at 20 KB with ``truncated: true`` and a
``next_offset`` to continue from; a command's output is only cut, and
says so. Sizes are measured as the answer serialises them (JSON,
``ensure_ascii=False``, UTF-8): escaped quotes and newlines count, and so
does every byte of an accented letter, no more.

20 KB keeps every answer under what Claude Code takes from a tool by
default, 25,000 tokens: a token stands for at least one byte of the text,
so an answer of fewer bytes than that has fewer tokens too, whatever it
holds (hex, escapes, other scripts). The 1 KB of paging keys a page may
add (``PAGE_SLACK``) and the untrusted wrapping leave more than 3 KB to
spare.
"""

import copy
import json

MAX_BYTES = 20 * 1024
#: Bytes between two entries of a list, as the answer serialises it: ``, ``.
SEPARATOR_BYTES = 2
#: The keys under which a paged answer carries its page of entries.
PAGE_LIST_KEYS = ("items", "entries", "alerts")
#: What a page that cannot show any entry tells the agent.
PAGE_TOO_SMALL = (
    "The rest of this answer leaves no room for its entries: none can be "
    "shown, and asking again from next_offset gives the same page."
)


def _too_small(page):
    """Mark *page* as one that shows no entry where entries remain."""
    page["page_too_small"] = True
    page["notice"] = PAGE_TOO_SMALL
    return page


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
    JSON text — and the page says ``truncated``. A page with no room for
    any of it is empty, its ``next_offset`` is that entry, and it says so
    (``page_too_small`` and a ``notice``).

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
            cut = True
            # No room even for a piece of it: the page stays empty and the
            # next one starts at this entry, neither invented nor skipped.
            if not item:
                break
            cost = _size(item) + SEPARATOR_BYTES
        page.append(item)
        size += cost
    following = offset + len(page)
    result = {
        "items": page,
        "offset": offset,
        "total": total,
        "truncated": cut or following < total,
        "next_offset": following if following < total else None,
    }
    return _too_small(result) if not page and offset < end else result


def page_lines(lines, offset=0, limit=None, page=None, max_bytes=MAX_BYTES):
    """Return one page of *lines*, with how many lines and pages there are.

    The pages are cut from the first line on: each holds the most lines
    that fit *max_bytes* as a list, *limit* at most; a line longer than a
    page is a page by itself, cut to fit (``truncated``). *page* returns
    one of them; *offset* starts at any line instead, and the page then
    holds as many lines from it as fit, so the next ones follow from
    ``next_offset`` without a gap. ``page`` says which page holds the
    first line shown. *lines* is read once, to its end, keeping only the
    page asked for.

    :param lines: an iterable of strings
    :param int offset: the first line to show, counted from 0
    :param limit: at most this many lines a page, or None
    :param page: the page to show, from 1, instead of *offset*
    :return: ``{items, offset, page, total, total_pages, truncated,
        next_offset}``; ``total`` counts the lines; ``page`` is None
        when *offset* is past the last line
    """
    limit = max(1, int(limit)) if limit else None
    start = None if page else max(0, int(offset or 0))
    items, size, full, cut = [], 2, False, False
    pages, page_size, page_count, holder, total = 0, 2, 0, None, 0
    for index, line in enumerate(lines):
        total = index + 1
        cost = _size(line) + SEPARATOR_BYTES
        if (
            not pages
            or page_count == limit
            or (page_count and page_size + cost > max_bytes)
        ):
            pages, page_size, page_count = pages + 1, 2, 0
            if page and pages == page:
                start = index
        page_size += cost
        page_count += 1
        if start is None or index < start or full:
            continue
        if index == start:
            holder = pages
        if items and (len(items) == limit or size + cost > max_bytes):
            full = True
            continue
        if not items and size + cost > max_bytes:
            # Alone on its page, as the pages are cut: nothing follows it.
            line, cut, full = _fit(line, max_bytes - size - SEPARATOR_BYTES), True, True
            cost = _size(line) + SEPARATOR_BYTES
        items.append(line)
        size += cost
    start = total if start is None else start
    following = start + len(items)
    return {
        "items": items,
        "offset": start,
        "page": holder,
        "total": total,
        "total_pages": pages,
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


def cap_strings(obj, max_bytes=MAX_BYTES, whole=frozenset()):
    """Cut every string inside *obj* longer than *max_bytes*.

    A string is measured as the answer serialises it, without the two
    quotes around it, and cut to its longest prefix that fits.

    :param whole: keys whose values are left whole, however long
    :return: ``(obj with long strings cut, whether any was cut)``
    """
    if isinstance(obj, str):
        cut = _fit(obj, max_bytes + 2)
        return cut, cut != obj
    if isinstance(obj, dict):
        pairs = {
            key: (item, False) if key in whole else cap_strings(item, max_bytes, whole)
            for key, item in obj.items()
        }
        return (
            {key: pair[0] for key, pair in pairs.items()},
            any(pair[1] for pair in pairs.values()),
        )
    if isinstance(obj, (list, tuple)):
        pairs = [cap_strings(item, max_bytes, whole) for item in obj]
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
    result = {
        **rest,
        key: page["items"],
        "offset": offset,
        "truncated": page["truncated"] or more,
        "next_offset": following if more else None,
    }
    return _too_small(result) if page.get("page_too_small") else result


#: Lists an answer gives up first, before any other is cut: the jobs
#: ``get_instance`` lists beside the instance's settings.
EXPENDABLE_KEYS = frozenset({"recentJobs", "activeJobs"})


def _head_lists(obj, count, whole=frozenset(), only=None):
    """Return *obj* with its lists cut to their first *count* entries.

    A list cut says so beside it, in its dict: ``<key>_truncated``.

    :param whole: keys whose values are left whole
    :param only: keys of the only lists to cut, or None for every list
    """
    if isinstance(obj, dict):
        result = {}
        for key, item in obj.items():
            if key in whole:
                result[key] = item
                continue
            cut_here = isinstance(item, (list, tuple)) and (
                only is None or key in only
            )
            if cut_here:
                result[key] = [_head_lists(entry, count, whole, only) for entry in item[:count]]
                if len(item) > count:
                    result[f"{key}_truncated"] = True
            else:
                result[key] = _head_lists(item, count, whole, only)
        return result
    if isinstance(obj, (list, tuple)):
        entries = obj if only is not None else obj[:count]
        return [_head_lists(item, count, whole, only) for item in entries]
    return obj


def _longest_list(obj, whole=frozenset(), only=None):
    """Return the length of the longest list inside *obj* that may be cut."""
    if isinstance(obj, dict):
        return max(
            (
                max(len(item), _longest_list(item, whole, only))
                if isinstance(item, (list, tuple)) and (only is None or key in only)
                else _longest_list(item, whole, only)
                for key, item in obj.items()
                if key not in whole
            ),
            default=0,
        )
    if isinstance(obj, (list, tuple)):
        inner = max((_longest_list(item, whole, only) for item in obj), default=0)
        return max(len(obj), inner) if only is None else inner
    return 0


def _most_entries(obj, max_bytes, whole=frozenset(), only=None):
    """Return *obj* with its lists cut to the most entries that fit, or None.

    :return: the cut answer, or None when even empty lists do not fit
    """
    if _size(_head_lists(obj, 0, whole, only)) > max_bytes:
        return None
    low, high = 0, _longest_list(obj, whole, only)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(_head_lists(obj, middle, whole, only)) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return _head_lists(obj, low, whole, only)


def _held_whole(obj, whole):
    """Return the keys of *whole* that *obj* holds, anywhere in it."""
    found, stack = set(), [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.update(key for key in node if key in whole)
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return found


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
    starts at the first of them, so none is skipped. The page is measured
    again with what it ends up saying — its new ``next_offset``, and the
    notice of a page that shows no entry — and gives up one more entry
    while that does not fit. When not even one entry fits whole, the first
    is cut to the room the page leaves it, as :func:`page_items` does, and
    the page says ``truncated``.

    :param obj: a copy of the answer, changed in place
    :return: whether it now fits; False when not even an empty page that
        says so fits
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
    kept, first_cut = entries[:low], False
    # A next_offset that grows from null to a long number, or the notice of
    # a page that shows nothing, can take it past the limit: one entry less,
    # and once no entry is left whole, a piece of the first, if one fits.
    while True:
        if not kept and entries and not first_cut:
            kept, first_cut = _cut_first(page, key, entries, obj, max_bytes), True
        page[key] = kept
        if len(kept) < len(entries):
            page["next_offset"] = page["offset"] + len(kept)
            page["truncated"] = True
        if not kept and entries:
            _too_small(page)
        if _size(obj) <= max_bytes:
            return True
        if not kept:
            return False
        kept = kept[:-1]


def _cut_first(page, key, entries, obj, max_bytes):
    """Return ``[the first of *entries*, cut to fit]``, or ``[]``.

    Cut to the room *page* leaves it once it says what it will — the
    entry after it is where the next page starts, and it is
    ``truncated`` — so that saying it takes no room the piece counted on.

    :param dict page: the page, inside *obj*, changed in place
    :param str key: the key of the page's entries
    :param list entries: all the entries the page had
    :param obj: the whole answer
    :param int max_bytes: what the whole answer must fit
    """
    page[key] = []
    if len(entries) > 1:
        page["next_offset"] = page["offset"] + 1
    page["truncated"] = True
    first = entries[0]
    text = first if isinstance(first, str) else _dumps(first)
    cut = _fit(text, max_bytes - _size(obj) - SEPARATOR_BYTES - 2)
    return [cut] if cut else []


def _cap_around_pages(obj):
    """Return a copy of *obj* with its strings cut to 1 KB, but its pages'
    entries, which keep theirs."""
    paged = copy.deepcopy(obj)
    found = _page_lists(paged)
    entries = [page[key] for page, key in found]
    for page, key in found:
        page[key] = []
    capped, _cut = cap_strings(paged, _FIELD_BYTES)
    for (page, key), kept in zip(_page_lists(capped), entries, strict=True):
        page[key] = kept
    return capped


def fit_answer(obj, max_bytes=MAX_BYTES, whole=frozenset()):
    """Return ``(obj, False)`` when *obj* fits *max_bytes*, else a cut of it.

    The safety net behind the tools that page their answers, which
    masking can still grow past the page. A page of entries loses its
    last entries and its ``next_offset`` moves back to the first of them:
    nothing is skipped; when what comes with the page is too big by itself,
    its strings are cut to 1 KB first. A page of text, whose offsets count the
    characters before masking, is left as it is. Any other answer gives up
    the lists it can do without first (:data:`EXPENDABLE_KEYS`), then has
    its lists cut, every one to the same number of entries, the most that
    fits; when even empty lists do not make it fit, strings are cut to 1 KB
    first. Each list cut says so beside it (``<key>_truncated``).

    What is under a key of *whole* is never cut, list or string: what an
    update writes back whole (an instance's repositories, domains,
    allowed hosts, dependencies), which an agent that rewrote it cut
    would lose. When the answer does not fit otherwise, it is an error
    that says so.

    :param whole: keys whose values are never cut
    :return: ``(answer that fits, whether it was cut)``
    """
    if _size(obj) <= max_bytes:
        return obj, False
    if _page_lists(obj):
        for paged in (copy.deepcopy(obj), _cap_around_pages(obj)):
            if _fit_page(paged, max_bytes):
                return paged, True
        # Even cut, what comes with the page does not fit: no entry is
        # skipped, as an answer without a page skips none.
        return {"truncated": True, "error": "The answer is too large to show."}, True
    if _has_next_offset(obj):
        return obj, False
    spared = _most_entries(obj, max_bytes, whole, EXPENDABLE_KEYS)
    if spared is not None:
        return spared, True
    obj = _head_lists(obj, 0, whole, EXPENDABLE_KEYS)
    for candidate in (obj, cap_strings(obj, _FIELD_BYTES, whole)[0]):
        cut = _most_entries(candidate, max_bytes, whole)
        if cut is not None:
            return cut, True
    held = sorted(_held_whole(obj, whole))
    if held:
        return {
            "truncated": True,
            "error": (
                "The answer is too large to show, and cutting it would cut "
                f"{', '.join(held)}, which an update writes back whole: ask "
                "for less at a time."
            ),
        }, True
    return {"truncated": True, "error": "The answer is too large to show."}, True
