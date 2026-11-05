"""Read the panel's scripts and templates for the calls they make.

What ``test_inventory`` holds against the registry: the routes ``rpc``
calls (and ``fetch``, ``window.open`` and ``location`` open), the model
methods ``orm`` calls, and the job types ``cloud.job.enqueue`` queues.
Comments are blanked first by a tokenizer that respects strings, template
literals and regular expressions: a call quoted in a comment or a JSDoc
example is not one. A call whose route or job type is not a literal comes
back as ``dynamic`` (``data_job`` for a job type), for the test to find it
declared.
"""

import re
from dataclasses import dataclass

#: Characters after which a ``/`` opens a regular expression, not a division.
_REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")
#: Words after which a ``/`` opens a regular expression.
_REGEX_AFTER_WORDS = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete"}
    | {"void", "throw", "yield", "await"}
)
#: The ``orm`` service's methods; ``call`` names the model's method itself.
_ORM_METHODS = (
    "call",
    "create",
    "write",
    "unlink",
    "read",
    "search",
    "nameSearch",
    "searchRead",
    "webSearchRead",
    "searchCount",
    "webRead",
    "webSave",
    "readGroup",
    "webReadGroup",
)
_ORM_RE = re.compile(r"(?<![\w$])orm\.(?:silent\.)?(" + "|".join(_ORM_METHODS) + r")\(")
_RPC_RE = re.compile(r"(?<![\w$])rpc\(")
_PAGE_RES = (
    re.compile(r"(?<![\w$])(?:window\.)?open\("),
    re.compile(r"(?<![\w$])fetch\("),
    re.compile(r"(?<![\w$.])location(?:\.href)?\s*=(?!=)"),
    re.compile(r"(?<![\w$.])location\.(?:assign|replace)\("),
)
_XML_ACTION_RE = re.compile(r"instanceAction\(\s*\{\s*code\s*:\s*'(\w+)'")


@dataclass(frozen=True)
class Call:
    """One call found.

    ``kind`` is ``rpc`` (a JSON-RPC route), ``page`` (a page or a file
    opened), ``orm`` (``<model>.<method>``), ``job`` (a job type's code)
    or ``dynamic`` (an ``rpc`` or ``orm`` call whose target is not a
    literal: ``value`` is the expression); ``data_job`` is a
    ``cloud.job.enqueue`` whose job type is not a literal (``action.code``):
    like ``dynamic``, its file declares the expression, and the job types
    offered from data answer for what it queues.
    """

    kind: str
    value: str
    line: int
    source: str = ""


def _string_end(text, start):
    """Return the index just past the string literal opening at *start*.

    A template literal's ``${…}`` may hold strings and templates of its own.
    """
    quote = text[start]
    i, n = start + 1, len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == quote:
            return i + 1
        if quote == "`" and text.startswith("${", i):
            i = _expression_end(text, i + 2)
            continue
        if quote != "`" and char == "\n":
            return i
        i += 1
    return n


def _expression_end(text, start):
    """Return the index just past the ``}`` closing a template's ``${``."""
    depth, i, n = 1, start, len(text)
    while i < n:
        char = text[i]
        if char in "'\"`":
            i = _string_end(text, i)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    return n


def _regex_end(text, start):
    """Return the index just past the regular expression opening at *start*."""
    i, n, in_class = start + 1, len(text), False
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "\n":
            return i
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            i += 1
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            return i
        i += 1
    return n


def _opens_regex(out):
    """Return whether a ``/`` after the code kept in *out* opens a regex."""
    code = "".join(out[-8:]).rstrip()
    if not code:
        return True
    if code[-1] in _REGEX_AFTER:
        return True
    word = re.search(r"([A-Za-z_$][\w$]*)$", code)
    return bool(word and word.group(1) in _REGEX_AFTER_WORDS)


def strip_comments(text):
    """Return *text* (a script) with its comments blanked, its lines kept.

    :param str text: JavaScript source
    :rtype: str
    """
    out, i, n = [], 0, len(text)
    while i < n:
        char = text[i]
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        elif char in "'\"`":
            end = _string_end(text, i)
            out.append(text[i:end])
            i = end
        elif char == "/" and _opens_regex(out):
            end = _regex_end(text, i)
            out.append(text[i:end])
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _arguments(text, start):
    """Return the arguments of the call whose ``(`` ends at *start*, as source.

    :return: the top-level arguments, stripped, up to the closing bracket
    :rtype: list[str]
    """
    args, depth, i, n, begin = [], 0, start, len(text), start
    while i < n:
        char = text[i]
        if char in "'\"`":
            i = _string_end(text, i)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if not depth:
                args.append(text[begin:i].strip())
                return [arg for arg in args if arg]
            depth -= 1
        elif char == "," and not depth:
            args.append(text[begin:i].strip())
            begin = i + 1
        i += 1
    return [arg for arg in args if arg]


def literal(source):
    """Return the text of the string literal *source*, or None.

    A template literal's ``${…}`` become ``1``: a path matched against the
    routing map takes it as any value.

    :rtype: str | None
    """
    if len(source) < 2 or source[0] not in "'\"`" or source[-1] != source[0]:
        return None
    if _string_end(source, 0) != len(source):
        return None
    body = source[1:-1]
    if source[0] == "`":
        parts, i = [], 0
        while (start := body.find("${", i)) != -1:
            parts.append(body[i:start])
            parts.append("1")
            i = _expression_end(body, start + 2)
        parts.append(body[i:])
        body = "".join(parts)
    return body


def _line(text, index):
    """Return the line number of *index* in *text*, from 1."""
    return text.count("\n", 0, index) + 1


def script_calls(text, wrappers=None):
    """Return the calls of the script *text*.

    :param str text: JavaScript source
    :param dict wrappers: ``{function: route prefix}``, helpers that call
        ``rpc`` for a route their first argument ends
    :rtype: list[Call]
    """
    code = strip_comments(text)
    calls = []
    for match in _RPC_RE.finditer(code):
        args = _arguments(code, match.end())
        line = _line(code, match.start())
        source = args[0] if args else ""
        path = literal(source)
        if path is None:
            calls.append(Call("dynamic", source, line, source))
        else:
            calls.append(Call("rpc", path, line, source))
    for name, prefix in (wrappers or {}).items():
        pattern = re.compile(r"(?<![\w$])" + re.escape(name) + r"\(")
        for match in pattern.finditer(code):
            args = _arguments(code, match.end())
            endpoint = literal(args[0]) if args else None
            if endpoint is not None and re.fullmatch(r"[\w/]+", endpoint):
                calls.append(
                    Call("rpc", prefix + endpoint, _line(code, match.start()), args[0])
                )
    for pattern in _PAGE_RES:
        for match in pattern.finditer(code):
            args = (
                _arguments(code, match.end())
                if code[match.end() - 1] == "("
                else [code[match.end() :].lstrip().split(";")[0].split("\n")[0]]
            )
            source = args[0].strip() if args else ""
            path = literal(source)
            if path and path.startswith("/cloud"):
                calls.append(Call("page", path, _line(code, match.start()), source))
    for match in _ORM_RE.finditer(code):
        calls.extend(_orm_calls(code, match))
    return calls


def _orm_calls(code, match):
    """Return the calls of one ``orm.<method>(`` of *code*."""
    verb = match.group(1)
    args = _arguments(code, match.end())
    line = _line(code, match.start())
    model = literal(args[0]) if args else None
    method = verb
    if verb == "call":
        method = literal(args[1]) if len(args) > 1 else None
    if not model or not method:
        source = ", ".join(args[:2])
        return [Call("dynamic", source, line, source)]
    if (model, method) != ("cloud.job", "enqueue"):
        return [Call("orm", f"{model}.{method}", line)]
    positional = args[2] if len(args) > 2 else ""
    items = _arguments(positional, 1) if positional.startswith("[") else []
    code_arg = items[2] if len(items) > 2 else ""
    job = literal(code_arg)
    if job:
        return [Call("job", job, line)]
    return [Call("data_job", code_arg, line, code_arg)]


def template_calls(text):
    """Return the job types the OWL template *text* queues through
    ``instanceAction({code: '…'})``, its comments left out.

    :param str text: the template's XML
    :rtype: list[Call]
    """
    code = re.sub(
        r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.DOTALL
    )
    return [
        Call("job", match.group(1), _line(code, match.start()))
        for match in _XML_ACTION_RE.finditer(code)
    ]
