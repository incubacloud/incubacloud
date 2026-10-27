"""The protocol versions this server speaks.

2026-07-28 is stateless: every request carries its version and the
client's capabilities in ``_meta``. The earlier ones open with
``initialize`` and keep what it negotiated in a session id.
"""

#: The stateless revision.
MODERN = "2026-07-28"
#: Revisions that open with ``initialize``, newest first.
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
#: Every version, newest first, as ``server/discover`` and the
#: unsupported-version error list them.
SUPPORTED = (MODERN, *LEGACY)


def negotiate_legacy(requested):
    """Return the version an ``initialize`` answers with.

    :param requested: the ``protocolVersion`` the client asked for
    :return: *requested* when this server speaks it, otherwise the newest
        legacy version (the client then decides whether to go on)
    :rtype: str
    """
    return requested if requested in LEGACY else LEGACY[0]
