"""Where an instance's backups are kept (its duplicity DST), without the ORM.

The destination reaches the hosts in three ways: as an argument of a
command (``backup_list``), as the ``DST`` line of an environment file
(the purges), and as an answer the template renders into a compose
file (deploy, rebuild, revive). The panel accepts it in the grammar
its own destinations are computed in: an S3 URL whose bucket and path
hold letters, digits, dots, hyphens and underscores only, or an
``sftp://`` URL of a user, a server and such a path, read as
``sftp_dst`` reads it.

Two readers of environment files: ``docker run --env-file`` (the
purges' files) takes the rest of a line as the value, as it is;
compose (``backup.env``, ``pitr.env``, each a service's ``env_file``)
reads more into it (:func:`compose_env_line`).
"""
import re
import unicodedata
from urllib.parse import urlparse

from ..net.hostname import is_host_address
from . import sftp_dst

#: ``boto3+s3://`` or ``s3://``, a bucket (with a port, for an endpoint
#: host spelled there), then path segments separated by one ``/``.
_DST_RE = re.compile(
    r"^(?:boto3\+s3|s3)://"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}"
    r"(?::[0-9]{1,5})?"
    r"(?:/[A-Za-z0-9._-]+)*/?$"
)

#: ``sftp://user@server[:port]``, then the path relative to the user's
#: home (``/``) or absolute (``//``), as ``sftp_dst.build`` writes it.
#: The server is a host name or an IPv4 address: what an SFTP
#: destination accepts.
_SFTP_DST_RE = re.compile(
    r"^sftp://[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}@[A-Za-z0-9.-]{1,253}"
    r"(?::[0-9]{1,5})?"
    r"(?:/{1,2}[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)?/?$"
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

#: An S3 endpoint: ``http`` or ``https`` in either case, a host name
#: (its labels may hold ``_``, as a container's name does, and it may
#: end in the ``.`` of a fully qualified name), an IPv4 address or a
#: bracketed IPv6 one, an optional port and an optional path of
#: unreserved characters. No user, query or fragment. None of these
#: characters means anything to an environment file, a shell or YAML.
_ENDPOINT_RE = re.compile(
    r"(?i:https?)://"
    r"(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
    r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*\.?"
    r"|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>[0-9]{1,5}))?"
    r"(?:/[A-Za-z0-9._~%/-]*)?"
)

#: A docker image tag, as the registry accepts one.
_IMAGE_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

#: An S3 access key ID or secret: base64's letters and the separators
#: providers use. Nothing an environment file or compose reads as more.
_ACCESS_KEY_RE = re.compile(r"[A-Za-z0-9/+=._-]+")

#: An S3 region, as providers name theirs (``us-east-1``, ``eu-west-par``,
#: ``auto``): nothing an environment file or compose reads as more.
_REGION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: What compose reads in an environment file as more than the value: a
#: quote that opens a value ends it, and what follows on the line is
#: read as another variable; ``$`` starts a variable it interpolates;
#: `` #`` starts a comment; ``\`` escapes in a quoted value; the
#: backquote is refused with them, as a shell would read it.
_COMPOSE_ENV_REFUSED = "\"'`$\\#"


def _port_in_range(port):
    """Whether *port*, digits or None, is a TCP port or absent.

    :param port: the digits matched, or None when no port was spelled
    :rtype: bool
    """
    return port is None or 0 < int(port) <= _MAX_PORT


def dst_is_valid(value):
    """Whether *value* is a backup destination the panel accepts.

    :param str value: a duplicity DST, such as
        ``boto3+s3://bucket/path/project/instance`` or
        ``sftp://user@server/path/project/instance``
    :return: True when it matches the grammar, has no ``.`` or ``..``
        segment and its port, if any, is at most 65535
    :rtype: bool
    """
    if value and value.startswith(f"{sftp_dst.SCHEME}://"):
        return _sftp_dst_is_valid(value)
    if not value or not _DST_RE.fullmatch(value):
        return False
    bucket, *segments = value.split("://", 1)[1].split("/")
    if "." in segments or ".." in segments:
        return False
    _name, _sep, port = bucket.partition(":")
    return _port_in_range(port or None)


def _sftp_dst_is_valid(value):
    """Whether *value* is an ``sftp://`` DST of the grammar.

    :param str value: such as ``sftp://u123@backup.example.net:23/b/p/i``
    :return: True when it matches ``_SFTP_DST_RE``, ``sftp_dst.parse``
        reads it and its server is a host name or an IPv4 address
    :rtype: bool
    """
    if not _SFTP_DST_RE.fullmatch(value):
        return False
    try:
        _user, host, _port, _path = sftp_dst.parse(value)
    except sftp_dst.DstError:
        return False
    return is_host_address(host)


def dst_key(value):
    """Return the bucket and the key parts a purge of *value* reads.

    The deletion program's own reading (``lib/purge_prefix.sh``): the
    scheme's ``boto3+`` dropped, the first part is the bucket and the
    rest, empty parts left out, is the prefix it empties. An ``sftp://``
    DST is read as ``sftp_dst.parse`` reads it: its server (user, host
    and port) stands for the bucket, and an absolute path starts with a
    ``/`` part, so it never reads as the same folder under the home.

    :param str value: a duplicity or wal-g destination
        (``boto3+s3://…``, ``s3://…`` or ``sftp://…``)
    :return: ``(bucket, parts)``, or None for a value with no bucket
    :rtype: tuple
    """
    if value and value.startswith(f"{sftp_dst.SCHEME}://"):
        try:
            user, host, port, path = sftp_dst.parse(value)
        except sftp_dst.DstError:
            return None
        parts = tuple(p for p in path.split("/") if p)
        if path.startswith("/"):
            parts = ("/", *parts)
        return f"{sftp_dst.SCHEME}://{user}@{host.lower()}:{port}", parts
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


def normalize_endpoint_url(value):
    """Return *value* with its scheme in lower case (``HTTPS://`` → ``https://``).

    What a destination stores: the scheme is case-insensitive, and the
    tools that read the endpoint compare it as written.

    :param value: the endpoint, or a falsy value
    :return: *value*, its scheme lowered when it has one
    """
    if not value or "://" not in value:
        return value
    scheme, sep, rest = value.partition("://")
    return scheme.lower() + sep + rest


def access_key_is_valid(value):
    """Whether *value* is an S3 access key ID or secret the panel accepts.

    :param str value: the key
    :return: True for letters, digits and ``/ + = . _ -`` only
    :rtype: bool
    """
    return bool(_ACCESS_KEY_RE.fullmatch(value or ""))


def region_is_valid(value):
    """Whether *value* is an S3 region the panel accepts.

    :param str value: such as ``eu-west-par``
    :return: True for letters, digits and ``. _ -``, up to 64, starting
        with a letter or a digit
    :rtype: bool
    """
    return bool(_REGION_RE.fullmatch(value or ""))


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
    """Return the line ``NAME=value`` of a file ``docker run --env-file`` reads.

    That reader takes a line at a time and the rest of the line as the
    value, quotes included: only a line break in *value* would start a
    variable of its own. Refused rather than escaped, as there is no
    escape. A file compose reads (``backup.env``, ``pitr.env``) takes
    :func:`compose_env_line`.

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


def compose_env_breaks(value):
    """Whether compose would read *value*, as a line's value, as something else.

    A line break or a control character, a character of
    ``_COMPOSE_ENV_REFUSED`` anywhere, or white space at the start or
    the end, which compose drops. A value of none of them reaches the
    container as it is written.

    :param str value: the value
    :rtype: bool
    """
    value = value or ""
    return (
        breaks_line(value)
        or any(ch in _COMPOSE_ENV_REFUSED for ch in value)
        or value != value.strip()
    )


def compose_env_line(name, value):
    """Return the line ``NAME=value`` of an environment file compose reads.

    Compose closes a value that opens with a quote and reads what
    follows on the line as another variable, interpolates ``$`` and cuts
    the value at `` #`` (:func:`compose_env_breaks`): the value would
    reach the container as something else, or name a variable of its
    own. Refused rather than escaped, as :func:`env_line` is.

    :param str name: the variable
    :param str value: its value
    :return: the line, without its line break
    :rtype: str
    :raises ValueError: when compose would not read *value* as it is
    """
    if compose_env_breaks(value):
        raise ValueError(
            f"{name} holds a quote, a $, a backslash, a #, a space at either "
            "end, a line break or a control character, which compose does "
            "not read as written in an environment file."
        )
    return f"{name}={value}"
