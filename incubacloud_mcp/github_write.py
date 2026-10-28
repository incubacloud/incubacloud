"""Writes to GitHub's REST API, for the agents' code tools.

The panel's GitHub client (``incubacloud/github/client.py``) reads; this
writes branches, commits, pull requests and repositories, with the same
rules: only ``api.github.com``, no redirect followed (``safe_urlopen``), a
JSON body, and an answer read up to a cap. The token is an App
installation token or the panel's personal access token: it goes in the
``Authorization`` header and nowhere else, never in a log or an error.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from odoo.addons.incubacloud.github.client import GitHubAPIError
from odoo.addons.incubacloud.github.http_utils import (
    read_json_limited,
    read_response_limited,
    safe_urlopen,
)

API = "https://api.github.com"
API_VERSION = "2022-11-28"
#: Seconds for each request.
TIMEOUT = 30
#: Largest answer read: a folder listing, or a 1 MB file in base64.
MAX_ANSWER_BYTES = 4 * 1024 * 1024
#: Largest error body read, of which the message keeps 200 characters.
MAX_ERROR_BYTES = 64 * 1024


def segment(value):
    """Return *value* quoted as one segment of an API path.

    :param value: an owner, a repository, a sha (attacker-controlled)
    :rtype: str
    """
    return urllib.parse.quote(str(value), safe="")


def path_of(value):
    """Return a path inside a repository, or a branch, quoted segment by segment.

    The slashes stay: ``assistant/internal-code`` is one branch, and
    ``models/res_partner.py`` one file.

    :rtype: str
    """
    return "/".join(segment(part) for part in str(value).split("/") if part)


class GitHubWriter:
    """One GitHub token, and the calls the code tools make with it."""

    def __init__(self, token):
        """Keep *token* for the requests; it is never shown."""
        self._token = token

    def request(self, method, endpoint, data=None):
        """Send one request to the API; return its decoded JSON answer.

        :param str method: ``GET``, ``POST``, ``PATCH`` or ``DELETE``
        :param str endpoint: the path after ``api.github.com``, quoted
        :param data: the JSON body, or None
        :return: the answer, ``{}`` when it has no body
        :raises GitHubAPIError: for any answer other than 2xx, with its status
        """
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            f"{API}{endpoint}",
            method=method,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "incubacloud/1.0",
            },
        )
        try:
            # B310: the address is fixed, https://api.github.com.
            with safe_urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310
                return read_json_limited(resp, max_bytes=MAX_ANSWER_BYTES)
        except urllib.error.HTTPError as exc:
            raw = read_response_limited(exc, max_bytes=MAX_ERROR_BYTES)
            raise GitHubAPIError(exc.code, raw.decode(errors="replace")) from exc

    def _repo(self, owner, name):
        """Return the API path of a repository."""
        return f"/repos/{segment(owner)}/{segment(name)}"

    def repository(self, owner, name):
        """Return the repository's description (``default_branch``, ``html_url``…)."""
        return self.request("GET", self._repo(owner, name))

    def head(self, owner, name, branch):
        """Return the sha of *branch*'s last commit.

        :raises GitHubAPIError: 404 when the branch does not exist
        """
        ref = self.request(
            "GET", f"{self._repo(owner, name)}/git/ref/heads/{path_of(branch)}"
        )
        return ref["object"]["sha"]

    def create_branch(self, owner, name, branch, sha):
        """Create *branch* at commit *sha*.

        :raises GitHubAPIError: 422 when the branch already exists
        """
        return self.request(
            "POST",
            f"{self._repo(owner, name)}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def commit_files(self, owner, name, branch, message, files):
        """Write *files* to *branch* in one commit; return the commit.

        Each file is ``{path, content}`` (the whole new text) or
        ``{path, delete: True}``. The ref moves only forward (``force``
        false): a push that came in between makes it fail, nothing is lost.

        :raises GitHubAPIError: as GitHub answers
        """
        base = self._repo(owner, name)
        head = self.head(owner, name, branch)
        tree = self.request("GET", f"{base}/git/commits/{segment(head)}")["tree"]["sha"]
        entries = [
            {"path": item["path"], "mode": "100644", "type": "blob", "sha": None}
            if item.get("delete")
            else {
                "path": item["path"],
                "mode": "100644",
                "type": "blob",
                "content": item["content"],
            }
            for item in files
        ]
        new_tree = self.request(
            "POST", f"{base}/git/trees", {"base_tree": tree, "tree": entries}
        )["sha"]
        commit = self.request(
            "POST",
            f"{base}/git/commits",
            {"message": message, "tree": new_tree, "parents": [head]},
        )
        self.request(
            "PATCH",
            f"{base}/git/refs/heads/{path_of(branch)}",
            {"sha": commit["sha"], "force": False},
        )
        return commit

    def open_pull(self, owner, name, head, base, title, body):
        """Open a pull request from *head* into *base*; return it."""
        return self.request(
            "POST",
            f"{self._repo(owner, name)}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def find_pull(self, owner, name, head):
        """Return the open pull request from *head* (``owner:branch``), or None."""
        found = self.request(
            "GET",
            f"{self._repo(owner, name)}/pulls?state=open&head={segment(head)}",
        )
        return found[0] if isinstance(found, list) and found else None

    def login(self):
        """Return the login of the account the token belongs to (a PAT's)."""
        return self.request("GET", "/user")["login"]

    def create_repository(self, owner, name, private, own_account):
        """Create a repository with a first commit (README); return it.

        :param bool own_account: True creates it in the token's own account
            (``/user/repos``), False in organization *owner*
        """
        endpoint = "/user/repos" if own_account else f"/orgs/{segment(owner)}/repos"
        return self.request(
            "POST",
            endpoint,
            {"name": name, "private": bool(private), "auto_init": True},
        )
