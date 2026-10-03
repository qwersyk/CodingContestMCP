import asyncio
import unittest
from unittest.mock import patch

import httpx

from ccc_mcp.client import APIError, CCCClient, contest_slug, game_origin
from ccc_mcp.config import Settings


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_csrf_bootstrap_and_cookie_isolation(self):
        seen = []

        def handle(request):
            seen.append(request)
            if request.url.path == "/api/games":
                return httpx.Response(
                    200,
                    json=[],
                    headers={"set-cookie": "XSRF-TOKEN=csrf%20token; Path=/"},
                )
            return httpx.Response(
                200, json={}, headers={"set-cookie": "unwanted=secret; Path=/"}
            )

        client = CCCClient(Settings(), "session-secret", httpx.MockTransport(handle))
        try:
            await client.json("POST", "/api/game-token", {"contestSlug": "test"})
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[1].headers["x-xsrf-token"], "csrf token")
            self.assertIn("SESSION=session-secret", seen[1].headers["cookie"])
            for _ in range(2):
                await client.game_json(
                    "https://birds.codingcontest.org/",
                    "/api/state",
                    {"Authorization": "game-token", "X-CCC-SLUG": "test"},
                )
                self.assertNotIn("cookie", seen[-1].headers)
                self.assertNotIn("x-xsrf-token", seen[-1].headers)
                self.assertEqual(seen[-1].headers["authorization"], "game-token")
            self.assertTrue(all("authorization" not in r.headers for r in seen[:2]))
        finally:
            await client.close()
        self.assertTrue(client.platform.is_closed)
        self.assertTrue(client.games.is_closed)

    async def test_upstream_errors_are_not_retried_or_rewritten(self):
        seen = []
        detail = {"error": "rate limited", "cooldownSec": 120, "cases": ["x" * 4000]}

        def handle(request):
            seen.append(request)
            return httpx.Response(429, json=detail, headers={"retry-after": "120"})

        client = CCCClient(Settings(), "session", httpx.MockTransport(handle))
        client.platform.cookies.set("XSRF-TOKEN", "csrf", domain="codingcontest.org")
        try:
            for _ in range(2):
                with self.assertRaises(APIError) as caught:
                    await client.json("POST", "/api/game-token", {})
                self.assertEqual(caught.exception.status, 429)
                self.assertEqual(caught.exception.detail, detail)
                self.assertEqual(caught.exception.retry_after, "120")
            self.assertEqual(len(seen), 2)
        finally:
            await client.close()

    async def test_redirects_and_non_json_errors(self):
        seen = []

        def handle(request):
            seen.append(request)
            return httpx.Response(
                302,
                text="original error",
                headers={"location": "https://other.example"},
            )

        client = CCCClient(Settings(), "session", httpx.MockTransport(handle))
        try:
            with self.assertRaises(APIError) as caught:
                await client.json("GET", "/api/contests/test")
            self.assertEqual(caught.exception.detail, "original error")
            self.assertEqual(len(seen), 1)
        finally:
            await client.close()

    async def test_game_state_reads_overlap(self):
        arrived = 0
        both = asyncio.Event()

        async def handle(request):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            return httpx.Response(200, json={"path": request.url.path})

        client = CCCClient(Settings(), "session", httpx.MockTransport(handle))
        try:
            responses = await asyncio.gather(
                client.game_json(
                    "https://birds.codingcontest.org", "/game/game-info", {}
                ),
                client.game_json(
                    "https://birds.codingcontest.org",
                    "/api/contestant/contestant-info",
                    {},
                ),
            )
            self.assertEqual(
                [r["path"] for r in responses],
                ["/game/game-info", "/api/contestant/contestant-info"],
            )
            self.assertIsNone(client.platform.timeout.read)
        finally:
            await client.close()


class ConfigurationTests(unittest.TestCase):
    def test_contest_links_and_slugs(self):
        self.assertEqual(contest_slug("training-2026.03"), "training-2026.03")
        for url in (
            "https://codingcontest.org/contests/test",
            "https://codingcontest.org/contests/test/game",
            "https://www.codingcontest.org/contests/test/game/",
        ):
            self.assertEqual(contest_slug(url), "test")
        for value in (
            "",
            ".",
            "..",
            "a/b",
            "a\\b",
            "a\nb",
            "https://example.org/contests/test/game",
            "https://codingcontest.org@evil.example/contests/test/game",
            "https://user@codingcontest.org/contests/test/game",
            "https://codingcontest.org/contests/%2Fapi%2Fauth/game",
            "https://codingcontest.org/contests/test/other",
            "https://codingcontest.org/challenges/test",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                contest_slug(value)

    def test_game_origins_cannot_redirect_credentials(self):
        self.assertEqual(
            game_origin("https://birds.codingcontest.org/"),
            "https://birds.codingcontest.org",
        )
        for value in (
            "https://example.com",
            "http://birds.codingcontest.org",
            "https://birds.codingcontest.org.evil.example",
            "https://birds.codingcontest.org@127.0.0.1",
            "https://user@birds.codingcontest.org",
            "https://birds.codingcontest.org:8443",
            "https://birds.codingcontest.org/foo",
            "https://birds.codingcontest.org/?token=secret",
            "https://www.codingcontest.org",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                game_origin(value)

    def test_server_settings_ignore_account_cookies_and_old_limits(self):
        with patch.dict(
            "os.environ",
            {
                "CCC_SESSION": "private-session",
                "CCC_COOKIE": "SESSION=private-session",
                "CCC_MAX_CONCURRENT_REQUESTS": "1",
                "CCC_MAX_FILE_BYTES": "1",
                "CCC_TIMEOUT": "",
                "MCP_PUBLIC_ORIGIN": "http://localhost:8000",
            },
        ):
            settings = Settings.from_env()
        self.assertIsNone(settings.timeout)
        self.assertFalse(hasattr(settings, "session"))
        self.assertFalse(hasattr(settings, "cookie"))
        self.assertFalse(hasattr(settings, "max_bytes"))

    def test_invalid_settings(self):
        for values in (
            {"port": 0},
            {"timeout": 0},
            {"public_origin": "https://host/path"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(**values)


if __name__ == "__main__":
    unittest.main()
