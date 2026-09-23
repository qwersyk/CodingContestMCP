import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from ccc_mcp.__main__ import download_http, upload_http
from ccc_mcp.app import create_app
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_limits_and_interruption_leave_no_artifacts(self):
        def factory(settings):
            return CCCClient(
                settings,
                httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"uuid": "user"})
                ),
            )

        async def oversized():
            yield b"x" * 50
            yield b"x" * 51

        async def interrupted():
            yield b"x" * 50
            raise httpx.ReadError("Upload interrupted")

        with tempfile.TemporaryDirectory() as root:
            app = create_app(Settings(data_dir=Path(root), max_bytes=100), factory)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"X-CCC-Session": "a" * 32},
                base_url="http://localhost",
            ) as http:
                for content in (b"x" * 101, oversized()):
                    response = await http.post("/mcp/artifacts", content=content)
                    self.assertEqual(response.status_code, 413)
                with self.assertRaises(httpx.ReadError):
                    await http.post("/mcp/artifacts", content=interrupted())
                self.assertFalse(any(p.is_file() for p in Path(root).rglob("*")))
                self.assertEqual(
                    (await http.post("/mcp/artifacts", content=b"x" * 100)).status_code,
                    200,
                )
                self.assertEqual(
                    (
                        await http.post(
                            "/mcp/artifacts",
                            content=b"x",
                            headers={"X-CCC-Session": "bad"},
                        )
                    ).status_code,
                    401,
                )

    async def test_round_trip_through_http_mcp(self):
        auth_calls = []

        def factory(settings):
            def handle(request):
                auth_calls.append(request.url.path)
                self.assertEqual(request.url.path, "/api/auth/current-user")
                return httpx.Response(200, json={"uuid": settings.session})

            return CCCClient(settings, httpx.MockTransport(handle))

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            payload = bytes(range(256)) * (20 * 1024 * 1024 // 256)
            source, target = root / "answer.out", root / "copy.out"
            source.write_bytes(payload)
            app = create_app(Settings(data_dir=root / "server"), factory)
            async with (
                app.app.router.lifespan_context(app.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    headers={"X-CCC-Session": "a" * 32},
                ) as http,
            ):
                metadata = await upload_http(
                    http, "http://localhost/mcp", source, len(payload)
                )
                artifact = metadata["artifact_id"]
                result = await download_http(
                    http, "http://localhost/mcp", artifact, target, len(payload)
                )
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(auth_calls, ["/api/auth/current-user"] * 2)
                self.assertEqual(metadata["sha256"], result["sha256"])
                self.assertIn("download_url", metadata)
                self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
                with self.assertRaises(ValueError):
                    await download_http(
                        http, "http://localhost/mcp", artifact, target, len(payload)
                    )
                self.assertEqual(target.read_bytes(), payload)
                limited = root / "limited.out"
                with self.assertRaises(ValueError):
                    await download_http(
                        http, "http://localhost/mcp", artifact, limited, 100
                    )
                self.assertFalse(limited.exists())
                empty = root / "empty"
                empty.touch()
                uploaded = await upload_http(http, "http://localhost/mcp", empty, 100)
                await download_http(
                    http,
                    "http://localhost/mcp",
                    uploaded["artifact_id"],
                    root / "empty-copy",
                    100,
                )
                self.assertEqual((root / "empty-copy").read_bytes(), b"")
                http.headers["X-CCC-Session"] = "b" * 32
                with self.assertRaises(httpx.HTTPStatusError):
                    await download_http(
                        http,
                        "http://localhost/mcp",
                        artifact,
                        root / "other-account",
                        len(payload),
                    )
                self.assertFalse((root / "other-account").exists())

    async def test_failure_leaves_no_partial_file_and_upload_limit_is_local(self):
        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 300000
                raise httpx.ReadError("Connection interrupted")

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "result"
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, stream=BrokenStream())
                )
            ) as http:
                with self.assertRaises(httpx.ReadError):
                    await download_http(
                        http, "https://example.com/mcp", "a" * 32, path, 1000000
                    )
            self.assertEqual(list(Path(root).iterdir()), [])
            path.write_bytes(b"too big")
            session = AsyncMock()
            with self.assertRaises(ValueError):
                await upload_http(session, "https://example.com/mcp", path, 2)
            session.post.assert_not_called()
