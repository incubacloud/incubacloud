"""HTTP utility helpers for GitHub API calls.

Centralises a no-redirect opener used by every ``urllib.request.urlopen``
in this codebase that talks to ``api.github.com``. Refusing redirects
closes the SSRF surface where a future API change (or a man-in-the-
middle on the route) could redirect the call to ``http://169.254.169.254``
or another internal address.
"""
import urllib.error
import urllib.request


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            "Redirects not allowed", headers, fp,
        )


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def safe_urlopen(req, timeout=10):
    """``urllib.request.urlopen`` equivalent that refuses redirects.

    Used for every fixed-host GitHub API call in this module so the
    target URL cannot be silently redirected to an attacker-controlled
    or internal-network address.
    """
    return _OPENER.open(req, timeout=timeout)
