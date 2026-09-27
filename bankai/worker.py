"""Worker process:  python -m bankai.worker   (tickets, handoffs, approval resume, expiry sweeper, session purge)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from .container import build_app
from .queueing.worker import Worker


async def _health_server(port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"status\":\"ok\"}\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "0.0.0.0", port)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = build_app()  # same composition root as the API, so the worker can resume paused conversations
    await app.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)  # graceful shutdown: finish the current message, then exit
        except NotImplementedError:  # Windows
            pass
    health = await _health_server(int(os.environ.get("HEALTH_PORT", "8090")))
    try:
        await Worker(app).run(stop)
    finally:
        health.close()
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
