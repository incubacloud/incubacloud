"""A download from the issuer ends at its deadline, whatever the server does."""

import socket
import threading
import time
from unittest.mock import patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.controllers import _fetch_json

from .common import http_response


class TestFetchJsonDeadline(BaseCase):
    """A real local server that drips its answer is cut at the deadline."""

    def _dripping_server(self, length, every):
        """Serve one answer of *length* announced bytes, one each *every* s.

        :return: the URL to fetch
        """
        server = socket.create_server(("127.0.0.1", 0))
        server.settimeout(10)
        stop = threading.Event()

        def serve():
            """Accept one request and drip its body until told to stop."""
            try:
                conn, _address = server.accept()
                with conn:
                    conn.recv(65536)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n" % length
                    )
                    for _i in range(length):
                        if stop.wait(every):
                            return
                        conn.sendall(b" ")
            except OSError:
                return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        # Cleanups run last first: stop the drip, wait for it, then close.
        self.addCleanup(server.close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        return f"http://127.0.0.1:{server.getsockname()[1]}/jwks"

    def test_a_body_dripped_under_its_announced_size_is_cut_at_the_deadline(self):
        """A byte every 0.1 s of 100 announced: cut at the deadline, not in 10 s."""
        url = self._dripping_server(length=100, every=0.1)
        start = time.monotonic()
        with (
            patch.object(_fetch_json, "DEADLINE_SECONDS", 0.5),
            self.assertRaises(ValueError) as caught,
        ):
            _fetch_json.fetch_json(url, 1024)
        elapsed = time.monotonic() - start
        self.assertIn("takes more than", str(caught.exception))
        self.assertLess(elapsed, 0.5 + _fetch_json.HTTP_TIMEOUT)

    def test_the_document_is_asked_as_is_and_refused_compressed(self):
        """A compressed answer is refused: its decoder reads past the deadline."""
        response = http_response(200, {"keys": []})
        response.headers["Content-Encoding"] = "gzip"
        with (
            patch.object(requests, "get", autospec=True, return_value=response) as get,
            self.assertRaises(ValueError),
        ):
            _fetch_json.fetch_json("https://idp.example.test/oauth/jwks", 1024)
        self.assertEqual(
            get.call_args.kwargs["headers"], {"Accept-Encoding": "identity"}
        )
        response.iter_content.assert_not_called()
