"""Writes to GitHub: what goes out, what comes back, and what never leaks."""

import io
import json
import urllib.error
from http.client import HTTPResponse
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.github.client import GitHubAPIError
from odoo.addons.incubacloud_mcp import github_write
from odoo.addons.incubacloud_mcp.github_write import GitHubWriter

TOKEN = "ghp_writer-test-token-0001"


def _answer(data, status=200):
    """A response ``safe_urlopen`` gives, with *data* as its JSON body."""
    body = b"" if data is None else json.dumps(data).encode()
    response = MagicMock(spec=HTTPResponse)
    response.__enter__.return_value = response
    response.status = status
    response.headers = {"Content-Length": str(len(body))}
    response.read.side_effect = lambda size=-1: body
    return response


def _refusal(status, data):
    """The ``HTTPError`` ``safe_urlopen`` raises for *status*."""
    body = json.dumps(data).encode()
    return urllib.error.HTTPError(
        "https://api.github.com/x", status, "refused", {}, io.BytesIO(body)
    )


class TestGitHubWriter(BaseCase):
    """Each call is one request to api.github.com, with the token and JSON."""

    def _sent(self, opener):
        """Return ``(method, url, headers, body)`` of each request sent."""
        sent = []
        for call in opener.call_args_list:
            req = call.args[0]
            body = json.loads(req.data) if req.data else None
            sent.append(
                (req.get_method(), req.full_url, dict(req.header_items()), body)
            )
        return sent

    def test_a_request_carries_the_token_and_json_and_reads_the_answer(self):
        """The token goes in Authorization, the data as JSON; the answer is decoded."""
        with patch.object(
            github_write, "safe_urlopen", autospec=True, return_value=_answer({"a": 1})
        ) as opener:
            answer = GitHubWriter(TOKEN).request("POST", "/repos/o/r/pulls", {"x": 2})
        self.assertEqual(answer, {"a": 1})
        method, url, headers, body = self._sent(opener)[0]
        self.assertEqual(
            (method, url), ("POST", "https://api.github.com/repos/o/r/pulls")
        )
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(headers["Content-type"], "application/json")
        self.assertEqual(body, {"x": 2})

    def test_a_refusal_is_an_api_error_without_the_token(self):
        """GitHub's refusal keeps its status; the token is nowhere in it."""
        refused = _refusal(403, {"message": "Resource not accessible"})
        with (
            patch.object(
                github_write, "safe_urlopen", autospec=True, side_effect=refused
            ),
            self.assertRaises(GitHubAPIError) as caught,
        ):
            GitHubWriter(TOKEN).request("GET", "/repos/o/r")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_an_empty_answer_is_an_empty_dict(self):
        """A 204 (deleted, updated) answers ``{}``."""
        with patch.object(
            github_write, "safe_urlopen", autospec=True, return_value=_answer(None, 204)
        ):
            self.assertEqual(GitHubWriter(TOKEN).request("DELETE", "/x"), {})

    def test_a_commit_of_several_files_is_one_tree_one_commit_and_the_ref(self):
        """Head, its tree, a new tree on top, the commit, the ref: in that order."""
        answers = [
            _answer({"object": {"sha": "head1"}}),
            _answer({"tree": {"sha": "tree1"}}),
            _answer({"sha": "tree2"}),
            _answer({"sha": "commit2", "html_url": "https://github.com/o/r/commit/2"}),
            _answer({"object": {"sha": "commit2"}}),
        ]
        with patch.object(
            github_write, "safe_urlopen", autospec=True, side_effect=answers
        ) as opener:
            commit = GitHubWriter(TOKEN).commit_files(
                "o",
                "r",
                "assistant/internal-code",
                "Add the internal code",
                [
                    {"path": "mod/__init__.py", "content": "from . import models\n"},
                    {"path": "old.txt", "delete": True},
                ],
            )
        self.assertEqual(commit["sha"], "commit2")
        sent = self._sent(opener)
        base = "https://api.github.com/repos/o/r"
        self.assertEqual(
            [(method, url) for method, url, _headers, _body in sent],
            [
                ("GET", f"{base}/git/ref/heads/assistant/internal-code"),
                ("GET", f"{base}/git/commits/head1"),
                ("POST", f"{base}/git/trees"),
                ("POST", f"{base}/git/commits"),
                ("PATCH", f"{base}/git/refs/heads/assistant/internal-code"),
            ],
        )
        tree = sent[2][3]
        self.assertEqual(tree["base_tree"], "tree1")
        self.assertEqual(
            tree["tree"],
            [
                {
                    "path": "mod/__init__.py",
                    "mode": "100644",
                    "type": "blob",
                    "content": "from . import models\n",
                },
                {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None},
            ],
        )
        self.assertEqual(sent[3][3]["parents"], ["head1"])
        self.assertEqual(sent[4][3], {"sha": "commit2", "force": False})

    def test_names_are_quoted_one_segment_at_a_time(self):
        """An owner or a branch with odd characters cannot change the path."""
        self.assertEqual(github_write.segment("a/b?c"), "a%2Fb%3Fc")
        self.assertEqual(github_write.path_of("a b/c#d"), "a%20b/c%23d")
