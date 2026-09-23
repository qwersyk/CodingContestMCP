import gzip
import io
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import httpx

from ccc_mcp.artifacts import Artifacts
from ccc_mcp.client import APIError, CCCClient, api_path, game_origin
from ccc_mcp.config import Settings
from ccc_mcp.service import Service


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_csrf_and_game_credential_isolation(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/api/games":
                return httpx.Response(
                    200, json=[], headers={"set-cookie": "XSRF-TOKEN=csrf; Path=/"}
                )
            return httpx.Response(
                200, json={}, headers={"set-cookie": "unwanted=secret; Path=/"}
            )

        client = CCCClient(
            Settings(session="platform-secret"), httpx.MockTransport(handler)
        )
        try:
            await client.json(
                "POST", "/api/game-token", json_body={"contestSlug": "test"}
            )
            self.assertEqual(requests[1].headers["x-xsrf-token"], "csrf")
            self.assertIn("platform-secret", requests[1].headers["cookie"])
            for _ in range(2):
                await client.request(
                    "POST",
                    "/api/submit",
                    origin="https://birds.codingcontest.org",
                    token="raw-token",
                    slug="test",
                    files={"solution": ("answer.out", b"42\n")},
                )
                request = requests[-1]
                self.assertNotIn("cookie", request.headers)
                self.assertEqual(request.headers["authorization"], "raw-token")
                self.assertEqual(request.headers["x-ccc-slug"], "test")
                self.assertIn(b'name="solution"', request.content)
                self.assertIn(b"42\n", request.content)
        finally:
            await client.close()

    async def test_gzip_and_expanded_size_limit(self):
        def handler(request):
            return httpx.Response(
                200,
                content=gzip.compress(b'{"value":42}'),
                headers={"content-encoding": "gzip"},
            )

        for limit in (1000, 5):
            client = CCCClient(Settings(max_bytes=limit), httpx.MockTransport(handler))
            try:
                if limit == 5:
                    with self.assertRaises(ValueError):
                        await client.json("GET", "/api/games")
                else:
                    self.assertEqual(
                        await client.json("GET", "/api/games"), {"value": 42}
                    )
            finally:
                await client.close()

    async def test_redirect_not_followed_and_mutation_not_retried(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://example.com"})

        client = CCCClient(
            Settings(cookie="XSRF-TOKEN=csrf"), httpx.MockTransport(handler)
        )
        try:
            with self.assertRaises(APIError):
                await client.request("POST", "/api/test")
            self.assertEqual(len(seen), 1)
        finally:
            await client.close()

    async def test_token_refresh_replays_only_reads(self):
        with tempfile.TemporaryDirectory() as root:
            tokens = []

            def handler(request):
                if request.url.path == "/api/contests/test":
                    return httpx.Response(
                        200,
                        json={
                            "slug": "test",
                            "gameBaseUrl": "https://birds.codingcontest.org",
                        },
                    )
                if request.url.path == "/api/game-token":
                    tokens.append(1)
                    return httpx.Response(200, json={"token": str(len(tokens))})
                if (
                    request.method == "POST"
                    or request.headers.get("authorization") == "1"
                ):
                    return httpx.Response(401, json={"error": "expired"})
                return httpx.Response(200, json={"ok": True})

            client = CCCClient(
                Settings(cookie="XSRF-TOKEN=csrf", data_dir=Path(root)),
                httpx.MockTransport(handler),
            )
            try:
                service = Service(client)
                self.assertEqual(
                    (await service.request("test", "GET", "/api/state")).status_code,
                    200,
                )
                self.assertEqual(len(tokens), 2)
                with self.assertRaises(APIError):
                    await service.request("test", "POST", "/api/submit")
                self.assertEqual(len(tokens), 2)
            finally:
                await client.close()


class ArtifactTests(unittest.TestCase):
    def test_paths_and_origins(self):
        for value in (
            "https://example.com",
            "http://birds.codingcontest.org",
            "https://birds.codingcontest.org@127.0.0.1",
            "https://birds.codingcontest.org/foo",
        ):
            with self.assertRaises(ValueError):
                game_origin(value)
        for value in (
            "/api/../admin",
            "/api/%252e%252e/admin",
            "//example.com/api/",
            "/api/hello\\world",
            "/api/test#fragment",
            "https://example.com/api/",
        ):
            with self.assertRaises(ValueError):
                api_path(value)
        self.assertEqual(api_path("/api/test?raw=true"), "/api/test?raw=true")

    def test_archive_paths_are_opaque(self):
        with tempfile.TemporaryDirectory() as root:
            artifacts = Artifacts(Path(root), 10000)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("../../escape.out", b"hello\x00world")
            stored = artifacts.save(buffer.getvalue(), "test.zip")
            self.assertEqual(
                artifacts.path(stored["artifact_id"]).read_bytes(), buffer.getvalue()
            )
            self.assertEqual(len(list(Path(root).iterdir())), 1)
            with self.assertRaises(ValueError):
                artifacts.path("../../escape.out")
            (Path(root) / ("f" * 32)).symlink_to("/etc/hosts")
            with self.assertRaises(ValueError):
                artifacts.path("f" * 32)

    def test_expiration_cleanup_only_removes_owned_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            account = root / "accounts" / ("a" * 64)
            artifacts = Artifacts(account, 1000, ttl_seconds=60)
            old = artifacts.save(b"old", "old.out")
            fresh = artifacts.save(b"new", "new.out")
            stale = time.time() - 120
            os.utime(account / old["artifact_id"], (stale, stale))
            partial = account / ("c" * 32 + ".part")
            partial.touch()
            os.utime(partial, (stale, stale))
            unrelated = account / "notes.txt"
            unrelated.write_text("keep")
            os.utime(unrelated, (stale, stale))
            external = root / "external"
            external.write_text("keep")
            os.utime(external, (stale, stale))
            (account / ("d" * 32)).symlink_to(external)
            with self.assertRaisesRegex(ValueError, "expired"):
                artifacts.path(old["artifact_id"])
            self.assertEqual(Artifacts.cleanup(root, 60), 2)
            self.assertTrue(artifacts.path(fresh["artifact_id"]).exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(external.exists())
            self.assertFalse(partial.exists())
            self.assertEqual(Artifacts.cleanup(root, 60), 0)


if __name__ == "__main__":
    unittest.main()
