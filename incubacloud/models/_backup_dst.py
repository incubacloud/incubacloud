"""Where an instance's backups are kept (its duplicity DST), without the ORM.

The destination reaches the hosts in three ways: as an argument of a
command (``backup_list``), as the ``DST`` line of an environment file
(the purges), and as an answer the template renders into a compose
file (deploy, rebuild, revive). The panel accepts it in one grammar,
the one its own destinations are computed in: an S3 URL whose bucket
and path hold letters, digits, dots, hyphens and underscores only.
"""
import re
import unicodedata
from urllib.parse import urlparse

#: ``boto3+s3://`` or ``s3://``, a bucket (with a port, for an endpoint
#: host spelled there), then path segments separated by one ``/``.
_DST_RE = re.compile(
    r"^(?:boto3\+s3|s3)://"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}"
    r"(?::[0-9]{1,5})?"
    r"(?:/[A-Za-z0-9._-]+)*/?$"
)


#: What the template's double-quoted compose scalar cannot carry as it
#: is: the quote ends it, the backslash escapes, and compose reads ``$``
#: as the start of a variable.
_COMPOSE_REFUSED = '"\\$'


def _breaks_line(ch):
    """Whether *ch* is a control character or a line or paragraph separator.

    :param str ch: one character
    :rtype: bool
    """
    category = unicodedata.category(ch)
    return category[0] == "C" or category in ("Zl", "Zp")


#: A port, where one is spelled: what a TCP port can be.
_MAX_PORT = 65535

#: An S3 endpoint: ``http`` or ``https``, a host name, an IPv4 address
#: or a bracketed IPv6 one, an optional port and an optional path of
#: unreserved characters. No user, query or fragment.
_ENDPOINT_RE = re.compile(
    r"https?://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>[0-9]{1,5}))?"
    r"(?:/[A-Za-z0-9._~%/-]*)?"
)

#: A docker image tag, as the registry accepts one.
_IMAGE_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def _port_in_range(port):
    """Whether *port*, digits or None, is a TCP port or absent.

    :param port: the digits matched, or None when no port was spelled
    :rtype: bool
    """
    return port is None or 0 < int(port) <= _MAX_PORT


def dst_is_valid(value):
    """Whether *value* is a backup destination the panel accepts.

    :param str value: a duplicity DST, such as
        ``boto3+s3://bucket/path/project/instance``
    :return: True when it matches the grammar, has no ``.`` or ``..``
        segment and its port, if any, is at most 65535
    :rtype: bool
    """
    if not value or not _DST_RE.fullmatch(value):
        return False
    bucket, *segments = value.split("://", 1)[1].split("/")
    if "." in segments or ".." in segments:
        return False
    _name, _sep, port = bucket.partition(":")
    return _port_in_range(port or None)


def dst_key(value):
    """Return the bucket and the key parts a purge of *value* reads.

    The deletion program's own reading (``lib/purge_prefix.sh``): the
    scheme's ``boto3+`` dropped, the first part is the bucket and the
    rest, empty parts left out, is the prefix it empties.

    :param str value: a duplicity or wal-g destination
        (``boto3+s3://…`` or ``s3://…``)
    :return: ``(bucket, parts)``, or None for a value with no bucket
    :rtype: tuple
    """
    parsed = urlparse((value or "").replace("boto3+s3://", "s3://", 1))
    if not parsed.netloc:
        return None
    return parsed.netloc, tuple(p for p in parsed.path.split("/") if p)


def dst_covers(prefix, path):
    """Whether emptying *prefix* reaches the objects kept under *path*.

    Part by part, as the purge deletes ``prefix/``: ``…/x`` covers
    ``…/x`` and ``…/x/y``, not ``…/x-pitr``.

    :param tuple prefix: what :func:`dst_key` returned for the prefix
    :param tuple path: what :func:`dst_key` returned for the path
    :rtype: bool
    """
    if not (prefix and path) or prefix[0] != path[0]:
        return False
    parts = prefix[1]
    return path[1][:len(parts)] == parts


def endpoint_url_is_valid(value):
    """Whether *value* is an S3 endpoint the panel accepts.

    :param str value: such as ``https://s3.example.net`` or
        ``http://10.0.0.5:9000``
    :return: True for an ``http``/``https`` URL of a host, an optional
        port up to 65535 and an optional path, with no space, control
        character, user, query or fragment
    :rtype: bool
    """
    match = _ENDPOINT_RE.fullmatch(value or "")
    return bool(match) and _port_in_range(match.group("port"))


def image_tag_is_valid(value):
    """Whether *value* is a docker image tag (``latest``, ``4.0.0``).

    :param str value: the tag
    :rtype: bool
    """
    return bool(_IMAGE_TAG_RE.fullmatch(value or ""))


def breaks_line(value):
    """Whether *value* holds a control character or a line separator.

    What a line of an environment file or a scalar of the compose file
    cannot carry as it is (:func:`env_line`).

    :param str value: the text
    :rtype: bool
    """
    return any(_breaks_line(ch) for ch in value or "")


def env_line(name, value):
    """Return the line ``NAME=value`` of an environment file.

    An environment file is read a line at a time and takes the rest of
    the line as the value, quotes included: a line break in *value*
    would start a variable of its own. Refused rather than escaped, as
    there is no escape.

    :param str name: the variable
    :param str value: its value
    :return: the line, without its line break
    :rtype: str
    :raises ValueError: when *value* holds a control character or a
        line or paragraph separator
    """
    if breaks_line(value):
        raise ValueError(
            f"{name} holds a line break or a control character, which an "
            "environment file cannot carry."
        )
    return f"{name}={value}"


def compose_safe(value):
    """Whether *value* can be rendered between the template's double quotes.

    :param str value: the destination answered to the template
    :return: False when it holds a double quote, a backslash, a ``$``, a
        control character or a line or paragraph separator
    :rtype: bool
    """
    return not any(ch in _COMPOSE_REFUSED or _breaks_line(ch) for ch in value)
