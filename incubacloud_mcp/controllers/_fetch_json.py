"""A bounded download of one of the issuer's small JSON documents.

The metadata document and the key set are a few kB. Whoever answers for
the issuer — or claims to — must not hold a worker or its memory with
them: no redirect is followed, the body is streamed and refused past its
cap, announced or read, and the body has a deadline (the ``timeout`` of
``requests`` bounds each read, not their sum).

The body is read a byte at a time and asked for uncompressed: a larger
read waits until it is full, and a decoder reads on until it has output,
so a server that drips its answer would hold the worker between two
checks of the deadline. This way the body ends, at the latest, one read
timeout after the deadline. Reading byte by byte costs about 4 ms a kB:
some 20 ms for a key set of 4 kB, half a second at a 128 kB cap.

What comes before the body — the status line, the headers and, in a
chunked answer, each chunk's size line — is read by ``requests`` itself,
and only the read timeout bounds it: a server that drips its headers a
byte every few seconds holds the request for as long as it keeps on.
Cutting that too would take a thread per download, left reading in the
background after the deadline; instead, the issuer is the one the
manager configured, it is asked at most every 30 seconds per process,
and on a server with workers ``limit_time_real`` ends such a request.
"""

import json
import time

import requests

#: Seconds to connect, and between two reads.
HTTP_TIMEOUT = 5
#: Seconds the whole download may take.
DEADLINE_SECONDS = 10


def fetch_json(url, max_bytes):
    """Return ``(status, document)`` of the JSON at *url*, read with bounds.

    An answer other than 200 is returned as ``(status, None)``, its body
    unread.

    :param str url: the https URL of the document
    :param int max_bytes: the largest body accepted
    :raises requests.RequestException: when the server cannot be reached
    :raises ValueError: when the body is too big, too slow, compressed or
        not JSON
    """
    start = time.monotonic()
    response = requests.get(
        url,
        timeout=HTTP_TIMEOUT,
        allow_redirects=False,
        stream=True,
        headers={"Accept-Encoding": "identity"},
    )
    try:
        if response.status_code != 200:
            return response.status_code, None
        encoding = str(response.headers.get("Content-Encoding") or "identity")
        if encoding.lower() != "identity":
            raise ValueError(f"{url} answers {encoding}-encoded, not as is")
        announced = str(response.headers.get("Content-Length") or "")
        if announced.isdigit() and int(announced) > max_bytes:
            raise ValueError(f"{url} announces {announced} bytes, over {max_bytes}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=1):
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"{url} sends more than {max_bytes} bytes")
            if time.monotonic() - start > DEADLINE_SECONDS:
                raise ValueError(f"{url} takes more than {DEADLINE_SECONDS} s")
        try:
            return 200, json.loads(bytes(body))
        # Nested past the parser's stack: as unreadable as any bad JSON,
        # and the callers remember a ValueError as the issuer's failure.
        except RecursionError as exc:
            raise ValueError(f"{url} nests its JSON too deep") from exc
    finally:
        response.close()
