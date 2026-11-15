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


def dst_is_valid(value):
    """Whether *value* is a backup destination the panel accepts.

    :param str value: a duplicity DST, such as
        ``boto3+s3://bucket/path/project/instance``
    :return: True when it matches the grammar and has no ``..`` segment
    :rtype: bool
    """
    if not value or not _DST_RE.fullmatch(value):
        return False
    return ".." not in value.split("://", 1)[1].split("/")


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
    if any(_breaks_line(ch) for ch in value):
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
