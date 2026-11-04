"""The view of a log an agent reads: no terminal codes, progress folded.

The same for every job's log, whatever ran it: the panel keeps the log as
it came, and only what an agent reads goes through here.

* Terminal escape sequences (colours, cursor moves, window titles) are
  taken out: ECMA-48 CSI and OSC sequences, and the other two- and
  three-byte escapes.
* A run of consecutive lines that are the same once their numbers are
  set aside — ``Extracting 1B``, ``Extracting 2B``…; ``Updating files:
  12% (120/1000)``… — shows as its last line, followed by
  ``(+N similar lines)``. A number is a run of digits that does not
  follow a letter, a digit or an underscore, with the digits after a
  ``.``, ``,`` or ``:`` that continue it (``1.5``, ``1,234``,
  ``00:01:23``): the hex of a layer id (``6e00e0164b46``) keeps its
  digits but its first, so two layers do not fold into one.

Folding only joins a line to the run before it: once a line is shown, the
lines before it keep their place however the log grows, and offsets
counted on this view stay valid while the job writes on.
"""

import re

#: OSC (up to BEL or ST), CSI (8-bit too) and every other escape sequence.
ANSI_ESCAPE = re.compile(
    r"""
    \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)
    | (?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]
    | \x1b[ -/]*[0-~]
    """,
    re.VERBOSE,
)
#: A number, as the folding sets it aside.
_NUMBER = re.compile(r"(?<!\w)\d+(?:[.,:]\d+)*")


def strip_ansi(line):
    """Return *line* without its terminal escape sequences."""
    return ANSI_ESCAPE.sub("", line)


def _shape(line):
    """Return *line* with every number replaced by ``0``."""
    return _NUMBER.sub("0", line)


def _folded(line, count):
    """Return the last *line* of a run of *count*, saying how many it hides."""
    if count == 1:
        return line
    hidden = count - 1
    return f"{line} (+{hidden} similar line{'s' if hidden > 1 else ''})"


def fold_similar(lines):
    """Yield *lines* with each run that differs only in numbers folded.

    :param lines: an iterable of lines
    :return: a generator of the folded lines
    """
    last, shape, count = None, None, 0
    for line in lines:
        current = _shape(line)
        if count and current == shape:
            last, count = line, count + 1
            continue
        if count:
            yield _folded(last, count)
        last, shape, count = line, current, 1
    if count:
        yield _folded(last, count)


def clean_lines(lines):
    """Yield the agent's view of *lines*: escapes out, then progress folded.

    :param lines: an iterable of the log's lines, as the panel keeps them
    :return: a generator of the view's lines
    """
    return fold_similar(strip_ansi(line) for line in lines)
