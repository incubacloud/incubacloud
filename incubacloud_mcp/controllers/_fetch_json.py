"""A bounded download of one of the issuer's small JSON documents.

The metadata document and the key set are a few kB. Whoever answers for
the issuer — or claims to — must not hold a worker or its memory with
them: no redirect is followed, the body is streamed and refused past its
cap, announced or read, and the whole download has a deadline (the
``timeout`` of ``requests`` bounds each read, not their sum).
"""

import json
import time

import requests

#: Seconds to connect, and between two reads.
HTTP_TIMEOUT = 5
#: Seconds the whole download may take.
DEADLINE_SECONDS = 10
_CHUNK_BYTES = 16 * 1024


def fetch_json(url, max_bytes):
    """Return ``(status, document)`` of the JSON at *url*, read with bounds.

    An answer other than 200 is returned as ``(status, None)``, its body
    unread.

    :param str url: the https URL of the document
    :param int max_bytes: the largest body accepted
    :raises requests.RequestException: when the server cannot be reached
    :raises ValueError: when the body is too big, too slow or not JSON
    """
    start = time.monotonic()
    response = requests.get(
        url, timeout=HTTP_TIMEOUT, allow_redirects=False, stream=True
    )
    try:
        if response.status_code != 200:
            return response.status_code, None
        announced = str(response.headers.get("Content-Length") or "")
        if announced.isdigit() and int(announced) > max_bytes:
            raise ValueError(f"{url} announces {announced} bytes, over {max_bytes}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"{url} sends more than {max_bytes} bytes")
            if time.monotonic() - start > DEADLINE_SECONDS:
                raise ValueError(f"{url} takes more than {DEADLINE_SECONDS} s")
        return 200, json.loads(bytes(body))
    finally:
        response.close()
