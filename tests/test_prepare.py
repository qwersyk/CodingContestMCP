import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from ccc_mcp.app import create_app
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings


class PrepareTests(unittest.TestCase):
    def test_preparation_and_binary_transfer_through_http(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("level-1.pdf", b"statement")
            archive.writestr("in_level-1_1-small.txt", b"5\nPR\nRR\nSS\nSR\nPS\n")
        calls = []
        submissions = []

        def factory(settings):
            def handle(request):
                calls.append(request.url.path)
                path = request.url.path
                if path == "/api/auth/current-user":
                    return httpx.Response(200, json={"uuid": settings.session})
                if path == "/api/contests/test":
                    data = {
                        "slug": "test",
                        "gameBaseUrl": "https://birds.codingcontest.org",
                    }
                elif path == "/api/game-token":
                    data = {"token": "token"}
                elif path == "/api/games":
                    return httpx.Response(
                        200, json=[], headers={"set-cookie": "XSRF-TOKEN=csrf; Path=/"}
                    )
                elif path == "/game/game-info":
                    data = {
                        "name": "Test",
                        "levelsInfo": {"levels": [{"inputFiles": ["1-small"]}]},
                    }
                elif path == "/api/contestant/contestant-info":
                    data = {
                        "score": {
                            "gameScore": {"level": 1},
                            "state": {
                                "level1": {
                                    "submissions": [{"fileId": "old"}] * 1000,
                                    "passedFiles": {"1-small": None},
                                }
                            },
                        }
                    }
                elif path == "/api/contestant/level/1/files":
                    return httpx.Response(
                        200,
                        content=payload.getvalue(),
                        headers={"content-type": "application/zip"},
                    )
                elif path == "/api/contestant/submit-1-1-small":
                    submissions.append(request.content)
                    data = {"evaluation": {"isCorrect": True}, "cooldownSec": 5}
                else:
                    self.fail(path)
                return httpx.Response(200, json=data)

            return CCCClient(settings, httpx.MockTransport(handle))

        with (
            tempfile.TemporaryDirectory() as root,
            TestClient(
                create_app(Settings(data_dir=Path(root)), factory),
                base_url="http://localhost",
            ) as client,
        ):
            headers = {
                "X-CCC-Session": "a" * 32,
                "Accept": "application/json, text/event-stream",
            }
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "prepare_level",
                        "arguments": {"contest": "test", "level": 1},
                    },
                },
            )
            result = response.json()["result"]
            self.assertFalse(result["isError"], result)
            data = result["structuredContent"]["data"]
            self.assertEqual(
                data["transfer"]["upload_url"], "http://localhost:8000/mcp/artifacts"
            )
            self.assertEqual(
                data["archive"]["download_url"],
                "http://localhost:8000/mcp/artifacts/" + data["archive"]["artifact_id"],
            )
            self.assertEqual(data["level_info"]["inputFiles"], ["1-small"])
            self.assertNotIn(
                "submissions", data["participant"]["score"]["state"]["level1"]
            )
            self.assertNotIn("files", data)
            self.assertEqual(len(list((Path(root) / "accounts").glob("*/*"))), 1)
            artifact = data["archive"]["artifact_id"]
            calls.clear()
            downloaded = client.get(f"/mcp/artifacts/{artifact}", headers=headers)
            self.assertEqual(downloaded.content, payload.getvalue())
            self.assertEqual(calls, ["/api/auth/current-user"])
            self.assertEqual(downloaded.headers["cache-control"], "no-store")
            self.assertEqual(client.get(f"/mcp/artifacts/{artifact}").status_code, 401)
            self.assertEqual(
                client.get(
                    f"/mcp/artifacts/{artifact}", headers={"X-CCC-Session": "b" * 32}
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(f"/mcp/artifacts/{artifact}", headers=headers).status_code,
                405,
            )

            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                pairs = archive.read("in_level-1_1-small.txt").decode().splitlines()[1:]
            answer = "\n".join(
                a if a == b or a + b in ("RS", "SP", "PR") else b for a, b in pairs
            ).encode()
            self.assertEqual(answer, b"P\nR\nS\nR\nS")
            uploaded = client.post(
                "/mcp/artifacts?filename=answer.out", headers=headers, content=answer
            )
            self.assertEqual(uploaded.status_code, 200)
            submitted = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "submit_solution",
                        "arguments": {
                            "contest": "test",
                            "level": 1,
                            "file_id": "1-small",
                            "artifact_id": uploaded.json()["data"]["artifact_id"],
                        },
                    },
                },
            ).json()["result"]
            self.assertFalse(submitted["isError"], submitted)
            self.assertTrue(
                submitted["structuredContent"]["data"]["evaluation"]["isCorrect"]
            )
            self.assertEqual(len(submissions), 1)
            self.assertIn(answer, submissions[0])
            catalog = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                },
            ).json()["result"]["tools"]
            names = {entry["name"] for entry in catalog}
            self.assertEqual(len(names), 13)
            self.assertFalse(
                names
                & {
                    "read_pdf",
                    "render_pdf_page",
                    "upload_artifact",
                    "read_artifact",
                    "download_level_files",
                }
            )
            schema = next(
                entry["inputSchema"]
                for entry in catalog
                if entry["name"] == "submit_solution"
            )
            self.assertIn("artifact_id", schema["required"])
            self.assertNotIn("solution", schema["properties"])
