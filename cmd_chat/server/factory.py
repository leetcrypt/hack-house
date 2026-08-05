import asyncio
import secrets
from contextlib import suppress
from sanic import Sanic
from sanic_ext import Extend
import os
from .managers import ConnectionManager
from .stores import MessageStore, UserSessionStore
from .srp_auth import SRPAuthManager
from .helpers import RateLimiter

from .routes import register_routes


def create_app(password: str = "", name: str = "cmd-chat-server") -> Sanic:
    app = Sanic(name)
    Extend(app)

    app.ctx.message_store = MessageStore()
    app.ctx.session_store = UserSessionStore()
    app.ctx.connection_manager = ConnectionManager()
    app.ctx.srp_manager = SRPAuthManager(password)
    app.ctx.room_salt = os.urandom(16)
    app.ctx.ws_secret = os.urandom(32)
    app.ctx.admin_token = secrets.token_hex(16)
    app.ctx.rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
    # Clergy capacity. 4 by default; raise via CMD_CHAT_MAX_USERS — infra-for-more,
    # the cap is data not architecture (broadcast fan-out is O(N)).
    app.ctx.max_users = int(os.environ.get("CMD_CHAT_MAX_USERS", "4"))
    app.ctx.cleanup_task = None

    register_lifecycle(app)
    register_routes(app)

    return app


def register_lifecycle(app: Sanic) -> None:
    @app.before_server_start
    async def setup(app: Sanic):
        app.ctx.cleanup_task = asyncio.create_task(cleanup_stale_sessions(app))

    @app.after_server_stop
    async def teardown(app: Sanic):
        if app.ctx.cleanup_task:
            app.ctx.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.ctx.cleanup_task


async def cleanup_stale_sessions(app: Sanic) -> None:
    # `suppress` wraps the loop, never a single iteration. Inside it, the
    # CancelledError raised in `asyncio.sleep` was swallowed and the loop went
    # straight back to sleeping — so `task.cancel()` in teardown could never
    # land, `await cleanup_task` blocked forever, and the worker hung at
    # "Stopping worker" instead of exiting. SIGTERM therefore did nothing: the
    # server could only be SIGKILLed, which is how test servers came to survive
    # for weeks on a developer box.
    with suppress(asyncio.CancelledError):
        while True:
            await asyncio.sleep(300)
            app.ctx.session_store.cleanup_stale()
