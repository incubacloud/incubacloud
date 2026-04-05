import asyncio
import concurrent.futures


def run_async(coro):
    """Run an asyncio coroutine safely from a synchronous context.

    Odoo HTTP workers may already have a running event loop (gevent/asyncio),
    so calling loop.run_until_complete() from the request thread raises
    "Cannot run the event loop while another loop is running".  Running in a
    fresh thread (which has no event loop) avoids the conflict entirely.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
