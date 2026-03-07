import os
import asyncio
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

async def start_health_server():
    """Minimal async health check server for Render web service."""
    async def handle(request):
        return web.Response(text="NatsuBot is running ✅")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on port {port}")
