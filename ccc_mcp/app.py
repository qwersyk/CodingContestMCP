"""Multi-account HTTP MCP. Website cookies are request-local; game tokens stay in RAM."""

import asyncio
import hashlib
import logging
import os
import re
from contextlib import suppress
from dataclasses import replace

import httpx
import uvicorn
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse

from . import game_tools  # noqa: F401 -- registers game tools
from .artifacts import StorageBudget, StorageFull
from .client import APIError, CCCClient
from .context import account_service
from .service import Service
from .sessions import AccountSessions
from .tools import create_mcp, settings


class AccountMiddleware:
    def __init__(self, app, configured, client_factory=CCCClient):
        self.app = app
        self.settings = configured
        self.client_factory = client_factory
        self.game_sessions = AccountSessions()
        self.slots = asyncio.Semaphore(configured.max_concurrent_requests)
        self.storage = StorageBudget(
            configured.data_dir.resolve(), configured.storage_max_bytes
        )

    async def cleanup_loop(self):
        while True:
            try:
                await asyncio.to_thread(
                    self.storage.cleanup,
                    self.settings.artifact_ttl_seconds,
                )
            except OSError:
                logging.getLogger(__name__).exception("Artifact cleanup failed")
            await asyncio.sleep(300)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.dispatch(scope, receive, send)
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=5)
        except TimeoutError:
            return await self.reject(
                scope, receive, send, 503, "Server busy; retry later", "5"
            )
        try:
            if scope["method"] == "POST" and scope["path"].rstrip("/") == "/mcp":
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 2 * 1024 * 1024:
                        return await self.reject(
                            scope,
                            receive,
                            send,
                            413,
                            "MCP arguments exceed 2 MiB; use binary HTTP upload",
                        )
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {
                            "type": "http.request",
                            "body": bytes(body),
                            "more_body": False,
                        }
                    return await receive()

                return await self.dispatch(scope, replay, send)
            return await self.dispatch(scope, receive, send)
        finally:
            self.slots.release()

    async def dispatch(self, scope, receive, send):
        if scope["type"] == "lifespan":
            cleanup = asyncio.create_task(self.cleanup_loop())
            try:
                return await self.app(scope, receive, send)
            finally:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        artifact_route = scope["path"].rstrip("/") == "/mcp/artifacts" or scope[
            "path"
        ].startswith("/mcp/artifacts/")
        if scope["path"].rstrip("/") != "/mcp" and not artifact_route:
            return await self.app(scope, receive, send)
        values = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"x-ccc-session"
        ]
        # Accept only the cookie value, never a Cookie header or arbitrary headers.
        if len(values) != 1 or not re.fullmatch(
            rb"[A-Za-z0-9_+/=%.-]{16,4096}", values[0]
        ):
            return await self.reject(
                scope,
                receive,
                send,
                401,
                "Supply your CCC SESSION cookie value in X-CCC-Session",
            )

        client = self.client_factory(
            replace(self.settings, cookie="", session=values[0].decode("ascii"))
        )
        try:
            try:
                user = await client.json("GET", "/api/auth/current-user")
                if (
                    not isinstance(user, dict)
                    or not isinstance(user.get("uuid"), str)
                    or not user["uuid"]
                ):
                    return await self.reject(
                        scope, receive, send, 401, "CCC session is not authenticated"
                    )
            except APIError as error:
                status = (
                    401
                    if error.status in (401, 403)
                    else 429
                    if error.status == 429
                    else 503
                )
                return await self.reject(
                    scope,
                    receive,
                    send,
                    status,
                    "CCC session expired or CCC authentication unavailable",
                    error.retry_after,
                )
            except (httpx.RequestError, ValueError):
                return await self.reject(
                    scope,
                    receive,
                    send,
                    503,
                    "CCC authentication unavailable; try later",
                )

            # Identity comes exclusively from CCC, not a caller-selected account ID.
            account = hashlib.sha256(user["uuid"].encode()).hexdigest()
            client.settings = replace(
                client.settings, data_dir=self.settings.data_dir / "accounts" / account
            )
            try:
                with self.game_sessions.use(account) as games:
                    service = Service(client, games, self.storage)
                    if artifact_route:
                        if scope["path"].rstrip("/") == "/mcp/artifacts":
                            if scope["method"] != "POST":
                                return await self.reject(
                                    scope,
                                    receive,
                                    send,
                                    405,
                                    "Use POST with a binary body",
                                )
                            request = Request(scope, receive)
                            filename = request.query_params.get(
                                "filename", "solution.out"
                            )
                            if len(filename) > 255 or any(
                                ord(c) < 32 for c in filename
                            ):
                                return await self.reject(
                                    scope, receive, send, 400, "Invalid filename"
                                )
                            try:
                                length = int(request.headers.get("content-length", "0"))
                            except ValueError:
                                return await self.reject(
                                    scope, receive, send, 400, "Invalid Content-Length"
                                )
                            if length < 0 or length > client.settings.max_bytes:
                                return await self.reject(
                                    scope,
                                    receive,
                                    send,
                                    413,
                                    "File exceeds CCC_MAX_FILE_BYTES",
                                )
                            try:
                                data = await service.artifacts.receive(
                                    request.stream(), filename
                                )
                            except ValueError as error:
                                return await self.reject(
                                    scope, receive, send, 413, str(error)
                                )
                            except ClientDisconnect:
                                return
                            except StorageFull as error:
                                return await self.reject(
                                    scope, receive, send, 507, str(error)
                                )
                            return await JSONResponse(
                                {"ok": True, "data": data},
                                headers={"Cache-Control": "no-store"},
                            )(scope, receive, send)
                        if scope["method"] not in ("GET", "HEAD"):
                            return await self.reject(
                                scope, receive, send, 405, "Use GET or HEAD"
                            )
                        try:
                            path = service.artifacts.path(
                                scope["path"].removeprefix("/mcp/artifacts/")
                            )
                        except ValueError:
                            return await self.reject(
                                scope, receive, send, 404, "Artifact not found"
                            )
                        return await FileResponse(
                            path,
                            media_type="application/octet-stream",
                            headers={
                                "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff",
                            },
                            filename=path.name,
                        )(scope, receive, send)
                    context_token = account_service.set(service)
                    try:
                        await self.app(scope, receive, send)
                    finally:
                        account_service.reset(context_token)
            except APIError as error:
                await self.reject(
                    scope,
                    receive,
                    send,
                    error.status,
                    str(error.detail),
                    error.retry_after,
                )
        finally:
            await client.close()

    @staticmethod
    async def reject(scope, receive, send, status, message, retry_after=None):
        headers = {"Cache-Control": "no-store"}
        if retry_after:
            headers["Retry-After"] = retry_after
        await JSONResponse(
            {"error": message, "retry_after": retry_after},
            status_code=status,
            headers=headers,
        )(scope, receive, send)


def create_app(configured=None, client_factory=CCCClient):
    return AccountMiddleware(
        create_mcp(configured or settings).streamable_http_app(),
        configured or settings,
        client_factory,
    )


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if os.getenv("MCP_TRANSPORT", "http") not in ("http", "streamable-http"):
        raise ValueError(
            "This multi-account server uses Streamable HTTP. Connect with X-CCC-Session."
        )
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        access_log=False,
        limit_concurrency=128,
    )
