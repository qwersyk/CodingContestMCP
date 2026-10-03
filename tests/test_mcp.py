import asyncio
import hashlib
import io
import json
import unittest
import zipfile
from contextlib import asynccontextmanager

import httpx

from ccc_mcp.app import create_app
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings


def token_for(session):
    return "game-" + hashlib.sha256(session.encode()).hexdigest()


def discovery_response(request, session):
    path = request.url.path
    if path.startswith("/api/contests/"):
        slug = path.rsplit("/", 1)[-1].replace(".", "-")
        return httpx.Response(
            200, json={"slug": slug, "gameBaseUrl": "https://birds.codingcontest.org/"}
        )
    if path == "/api/games":
        return httpx.Response(
            200, json=[], headers={"set-cookie": "XSRF-TOKEN=csrf; Path=/"}
        )
    if path == "/api/game-token":
        return httpx.Response(200, json={"token": token_for(session)})
    raise AssertionError(f"Unexpected discovery request: {path}")


@asynccontextmanager
async def harness(handler=discovery_response):
    created, seen = [], []

    def factory(session):
        async def handle(request):
            seen.append(request)
            value = handler(request, session)
            return await value if hasattr(value, "__await__") else value

        client = CCCClient(session, httpx.MockTransport(handle))
        created.append(client)
        return client

    app = create_app(Settings(), factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http,
    ):
        yield http, created, seen


def data_of(result):
    return json.loads(result["content"][0]["text"])


async def rpc(http, method, params=None, headers=None):
    return await http.post(
        "/mcp",
        headers=headers
        or {"Accept": "application/json, text/event-stream", "X-CCC-Session": "a" * 32},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


async def connect(
    http,
    contest="https://codingcontest.org/contests/training-test.01/game",
    session="a" * 32,
):
    response = await rpc(
        http,
        "tools/call",
        {"name": "connect_contest", "arguments": {"contest": contest}},
        {"Accept": "application/json, text/event-stream", "X-CCC-Session": session},
    )
    if response.status_code != 200:
        raise AssertionError(response.text)
    return response.json()["result"]


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_needs_no_ccc_call_and_has_self_contained_instructions(
        self,
    ):
        async with harness() as (http, created, seen):
            initialized = await rpc(
                http,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
                {"Accept": "application/json, text/event-stream"},
            )
            self.assertEqual(initialized.status_code, 200, initialized.text)
            self.assertNotIn("instructions", initialized.json()["result"])
            listed = await rpc(http, "tools/list")
            tools = listed.json()["result"]["tools"]
            instructions = tools[0]["description"]
            self.assertIn("multipart/form-data", instructions)
            self.assertIn("X-CCC-Session", instructions)
            self.assertIn("progress.cooldowns", instructions)
            self.assertIn("concurrent writes can overwrite progress", instructions)
            self.assertIn("non-null", instructions)
            self.assertEqual([t["name"] for t in tools], ["connect_contest"])
            self.assertEqual(tools[0]["inputSchema"]["required"], ["contest"])
            self.assertNotIn("ctx", tools[0]["inputSchema"]["properties"])
            self.assertEqual(created, [])
            self.assertEqual(seen, [])

    async def test_connection_returns_only_direct_http_routes(self):
        async with harness() as (http, clients, seen):
            result = await connect(http)
            self.assertFalse(result["isError"])
            data = data_of(result)
            self.assertEqual(data["headers"]["Authorization"], token_for("a" * 32))
            self.assertEqual(data["headers"]["Referer"], "https://codingcontest.org/")
            self.assertEqual(data["headers"]["X-CCC-SLUG"], "training-test-01")
            self.assertEqual(set(data), {"base_url", "headers", "paths"})
            self.assertNotIn("structuredContent", result)
            self.assertTrue(
                data["paths"]["files"].endswith("/level/{level}/files?raw=true")
            )
            self.assertNotIn("a" * 32, str(data))
            self.assertEqual(len(seen), 3)
            self.assertTrue(all(c.platform.is_closed for c in clients))
            self.assertEqual((await http.get("/mcp/artifacts")).status_code, 404)

    async def test_missing_invalid_and_duplicate_credentials_are_rejected(self):
        async with harness() as (http, clients, seen):
            for extra in (
                [],
                [("X-CCC-Session", "bad cookie\n")],
                [("X-CCC-Session", "one"), ("X-CCC-Session", "two")],
            ):
                response = await rpc(
                    http,
                    "tools/call",
                    {"name": "connect_contest", "arguments": {"contest": "test"}},
                    [("Accept", "application/json, text/event-stream"), *extra],
                )
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(data_of(result)["status"], 401)
            self.assertEqual(clients, [])
            self.assertEqual(seen, [])

    async def test_invalid_session_is_rejected_by_ccc_before_game_access(self):
        def handle(request, session):
            if request.url.path == "/api/game-token":
                return httpx.Response(401, json={"error": "session expired"})
            return discovery_response(request, session)

        async with harness(handle) as (http, clients, seen):
            result = await connect(http)
            self.assertTrue(result["isError"])
            self.assertEqual(data_of(result)["status"], 401)
            self.assertEqual(len(seen), 3)
            self.assertTrue(all(r.url.host == "codingcontest.org" for r in seen))
            self.assertTrue(clients[0].platform.is_closed)

    async def test_hostile_game_origin_never_receives_credentials(self):
        def handle(request, session):
            if request.url.path.startswith("/api/contests/"):
                return httpx.Response(
                    200, json={"slug": "test", "gameBaseUrl": "https://evil.example/"}
                )
            return discovery_response(request, session)

        async with harness(handle) as (http, _, seen):
            result = await connect(http)
            self.assertTrue(result["isError"])
            self.assertEqual(len(seen), 1)
            self.assertTrue(all(r.url.host == "codingcontest.org" for r in seen))

    async def test_429_does_not_block_next_connection_or_change_error(self):
        count = 0
        detail = {"error": "limited", "cooldownSec": 120, "extra": {"long": "x" * 5000}}

        def handle(request, session):
            nonlocal count
            if request.url.path == "/api/game-token":
                count += 1
                if count == 1:
                    return httpx.Response(
                        429, json=detail, headers={"retry-after": "120"}
                    )
            return discovery_response(request, session)

        async with harness(handle) as (http, _, _seen):
            first = await connect(http)
            self.assertEqual(
                data_of(first),
                {"status": 429, "error": detail, "retry_after": "120"},
            )
            self.assertTrue(first["isError"])
            second = await connect(http)
            self.assertFalse(second["isError"])
            self.assertEqual(count, 2)

    async def test_parallel_callers_keep_their_own_accounts_and_have_no_eight_call_limit(
        self,
    ):
        arrived = 0
        all_ready = asyncio.Event()

        async def handle(request, session):
            nonlocal arrived
            if request.url.path.startswith("/api/contests/"):
                arrived += 1
                if arrived == 12:
                    all_ready.set()
                await asyncio.wait_for(all_ready.wait(), 3)
            return discovery_response(request, session)

        async with harness(handle) as (http, clients, _):
            sessions = [f"{n:032d}" for n in range(12)]
            results = await asyncio.gather(
                *(connect(http, session=s) for s in sessions)
            )
            self.assertEqual(
                [data_of(r)["headers"]["Authorization"] for r in results],
                [token_for(s) for s in sessions],
            )
            self.assertTrue(all(not r["isError"] for r in results))
            self.assertTrue(all(c.platform.is_closed for c in clients))

    async def test_large_discovery_metadata_is_not_returned(self):
        def handle(request, session):
            response = discovery_response(request, session)
            if request.url.path.startswith("/api/contests/"):
                return httpx.Response(
                    200, json={**response.json(), "description": "x" * 100000}
                )
            return response

        async with harness(handle) as (http, _, seen):
            result = await connect(http)
            self.assertLess(len(json.dumps(result)), 1000)
            self.assertNotIn("structuredContent", result)
            self.assertTrue(all(r.url.host == "codingcontest.org" for r in seen))

    async def test_expired_token_is_refreshed_by_new_connection(self):
        count = 0

        def handle(request, session):
            nonlocal count
            if request.url.path == "/api/game-token":
                count += 1
                return httpx.Response(200, json={"token": str(count)})
            return discovery_response(request, session)

        async with harness(handle) as (http, _, _):
            self.assertFalse((await connect(http))["isError"])
            refreshed = await connect(http)
            self.assertFalse(refreshed["isError"])
            self.assertEqual(
                data_of(refreshed)["headers"]["Authorization"],
                "2",
            )
            self.assertEqual(count, 2)

    async def test_network_error_does_not_leak_session_or_retry_mutation(self):
        token_calls = 0

        def handle(request, session):
            nonlocal token_calls
            if request.url.path == "/api/game-token":
                token_calls += 1
                raise httpx.ReadError("failed with private " + session, request=request)
            return discovery_response(request, session)

        async with harness(handle) as (http, clients, _):
            result = await connect(http)
            self.assertTrue(result["isError"])
            self.assertNotIn("a" * 32, str(result))
            self.assertEqual(token_calls, 1)
            self.assertTrue(clients[0].platform.is_closed)

    async def test_download_and_parallel_file_uploads_bypass_mcp(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("input.txt", "42\n")
            z.writestr("level-1.pdf", b"statement")
        arrived = 0
        both_uploads = asyncio.Event()
        uploads = []

        async with harness() as (http, _clients, seen):
            connection = data_of(await connect(http))
            discovery_count = len(seen)

            async def direct(request):
                nonlocal arrived
                self.assertEqual(request.url.host, "birds.codingcontest.org")
                self.assertEqual(request.headers["authorization"], token_for("a" * 32))
                self.assertNotIn("cookie", request.headers)
                if request.method == "GET":
                    self.assertEqual(request.url.params["raw"], "true")
                    return httpx.Response(
                        200,
                        content=archive.getvalue(),
                        headers={"content-type": "application/zip"},
                    )
                uploads.append(request.content)
                self.assertIn(b'name="solution"', request.content)
                arrived += 1
                if arrived == 2:
                    both_uploads.set()
                await asyncio.wait_for(both_uploads.wait(), 2)
                return httpx.Response(
                    200, json={"evaluation": {"isCorrect": True}, "cooldownSec": 4}
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(direct),
                headers=connection["headers"],
                base_url=connection["base_url"],
            ) as game:
                zip_response = await game.get(
                    connection["paths"]["files"].format(level=1)
                )
                with zipfile.ZipFile(io.BytesIO(zip_response.content)) as z:
                    self.assertEqual(z.read("input.txt"), b"42\n")
                outputs = [b"42\n", b"x" * (3 * 1024 * 1024)]
                results = await asyncio.gather(
                    *(
                        game.post(
                            connection["paths"]["submit"].format(
                                level=1, file_id=file_id
                            ),
                            files={
                                "solution": (
                                    "answer.out",
                                    output,
                                    "application/octet-stream",
                                )
                            },
                        )
                        for file_id, output in zip(("1-small", "2-large"), outputs)
                    )
                )
                self.assertTrue(
                    all(r.json()["evaluation"]["isCorrect"] for r in results)
                )
                self.assertEqual(len(uploads), 2)
                self.assertTrue(any(outputs[1] in upload for upload in uploads))
                self.assertEqual(len(seen), discovery_count)


if __name__ == "__main__":
    unittest.main()
