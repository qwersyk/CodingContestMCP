import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from ccc_mcp.app import create_app
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings


class HTTPTests(unittest.TestCase):
    def test_cleanup_starts_with_server_lifespan(self):
        cleaned = threading.Event()
        with (
            tempfile.TemporaryDirectory() as root,
            patch(
                "ccc_mcp.app.StorageBudget.cleanup",
                side_effect=lambda *args: cleaned.set(),
            ) as cleanup,
        ):
            with TestClient(create_app(Settings(data_dir=Path(root)))):
                self.assertTrue(cleaned.wait(2))
            cleanup.assert_called_once_with(21600)

    def test_multiple_accounts_and_artifact_isolation(self):
        clients = []
        sessions = {"a" * 32: "user-a", "b" * 32: "user-b", "c" * 32: "user-a"}

        def factory(settings):
            def handler(request):
                user = sessions.get(settings.session)
                if not user:
                    return httpx.Response(401, json={})
                return httpx.Response(200, json={"uuid": user})

            client = CCCClient(settings, httpx.MockTransport(handler))
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as root:
            configured = Settings(
                data_dir=Path(root),
                session="owner-secret-ignored",
                cookie="SESSION=owner-secret-ignored",
            )
            with TestClient(
                create_app(configured, factory), base_url="http://localhost"
            ) as client:

                def call(session, name, arguments=None):
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session:
                        headers["X-CCC-Session"] = session
                    return client.post(
                        "/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments or {}},
                        },
                    )

                self.assertEqual(call(None, "auth_status").status_code, 401)
                self.assertEqual(
                    call("invalid-session-value", "auth_status").status_code, 401
                )
                for session, user in sessions.items():
                    response = call(session, "auth_status")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(
                        response.json()["result"]["structuredContent"]["data"]["uuid"],
                        user,
                    )
                uploaded = client.post(
                    "/mcp/artifacts",
                    headers={"X-CCC-Session": "a" * 32},
                    content=b"private answer",
                )
                artifact = uploaded.json()["data"]["artifact_id"]
                for session, denied in [
                    ("b" * 32, True),
                    ("a" * 32, False),
                    ("c" * 32, False),
                ]:
                    read = client.get(
                        f"/mcp/artifacts/{artifact}", headers={"X-CCC-Session": session}
                    )
                    self.assertEqual(read.status_code, 404 if denied else 200)
                self.assertTrue(all(c.platform.is_closed for c in clients))
                self.assertEqual(len(list((Path(root) / "accounts").iterdir())), 2)

    def test_environment_credentials_are_ignored(self):
        with patch.dict(
            "os.environ",
            {
                "CCC_SESSION": "secret",
                "CCC_COOKIE": "SESSION=secret",
                "MCP_ACCESS_TOKEN": "old-secret",
            },
        ):
            settings = Settings.from_env()
        self.assertFalse(settings.session)
        self.assertFalse(settings.cookie)


class ConcurrentHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_calls_keep_request_context(self):
        ready = asyncio.Event()
        arrived = 0
        seen_clients = []

        def factory(settings):
            async def handler(request):
                nonlocal arrived
                if request.url.path == "/api/auth/current-user":
                    return httpx.Response(200, json={"uuid": settings.session})
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=3)
                return httpx.Response(200, json={"account": settings.session})

            client = CCCClient(settings, httpx.MockTransport(handler))
            seen_clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as root:
            app = create_app(Settings(data_dir=Path(root)), factory)
            async with app.app.router.lifespan_context(app.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost"
                ) as client:

                    async def call(session):
                        response = await client.post(
                            "/mcp",
                            headers={
                                "X-CCC-Session": session,
                                "Accept": "application/json, text/event-stream",
                            },
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {"name": "list_challenges", "arguments": {}},
                            },
                        )
                        return response.json()["result"]["structuredContent"]["data"][
                            "account"
                        ]

                    self.assertEqual(
                        await asyncio.gather(call("a" * 32), call("b" * 32)),
                        ["a" * 32, "b" * 32],
                    )
            self.assertTrue(all(c.platform.is_closed for c in seen_clients))
