import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ccc_mcp.__main__ import download, upload
from ccc_mcp.app import create_app
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_through_http_mcp(self):
        def factory(settings):
            def handle(request):
                self.assertEqual(request.url.path, "/api/auth/current-user")
                return httpx.Response(200, json={"uuid": settings.session})

            return CCCClient(settings, httpx.MockTransport(handle))

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            payload = bytes(range(256)) * 2048
            source, target = root / "answer.out", root / "copy.out"
            source.write_bytes(payload)
            app = create_app(Settings(data_dir=root / "server"), factory)
            async with (
                app.app.router.lifespan_context(app.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    headers={"X-CCC-Session": "a" * 32},
                ) as http,
                streamable_http_client("http://localhost/mcp", http_client=http) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                metadata = await upload(session, source, len(payload))
                artifact = metadata["artifact_id"]
                result = await download(session, artifact, target, len(payload))
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
                with self.assertRaises(ValueError):
                    await download(session, artifact, target, len(payload))
                self.assertEqual(target.read_bytes(), payload)
                limited = root / "limited.out"
                with self.assertRaises(ValueError):
                    await download(session, artifact, limited, 100)
                self.assertFalse(limited.exists())
                empty = root / "empty"
                empty.touch()
                uploaded = await upload(session, empty, 100)
                await download(
                    session, uploaded["artifact_id"], root / "empty-copy", 100
                )
                self.assertEqual((root / "empty-copy").read_bytes(), b"")
                http.headers["X-CCC-Session"] = "b" * 32
                with self.assertRaises(ValueError):
                    await download(
                        session, artifact, root / "other-account", len(payload)
                    )
                self.assertFalse((root / "other-account").exists())

    async def test_failure_leaves_no_partial_file_and_upload_limit_is_local(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "result"
            first = {
                "data": "YQ==",
                "offset": 0,
                "bytes": 1,
                "total_bytes": 2,
                "next_offset": 1,
            }
            for failure in (ValueError("Transfer failed"), asyncio.CancelledError()):
                with (
                    patch(
                        "ccc_mcp.__main__.call",
                        new=AsyncMock(side_effect=[first, failure]),
                    ),
                    self.assertRaises(type(failure)),
                ):
                    await download(None, "artifact", path, 100)
                self.assertEqual(list(Path(root).iterdir()), [])
            path.write_bytes(b"too big")
            session = AsyncMock()
            with self.assertRaises(ValueError):
                await upload(session, path, 2)
            session.call_tool.assert_not_called()
