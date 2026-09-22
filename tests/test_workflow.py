import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from ccc_mcp import game_tools
from ccc_mcp.client import APIError, CCCClient
from ccc_mcp.config import Settings
from ccc_mcp.service import Service


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_failed_cases_have_bounded_previews_and_full_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            client = CCCClient(
                Settings(data_dir=Path(root)),
                httpx.MockTransport(lambda _: self.fail("Unexpected request")),
            )
            service = Service(client)
            feedback = {
                "evaluation": {
                    "isCorrect": False,
                    "cases": [{"isCorrect": True}]
                    + [{"isCorrect": False, "actual": "x" * 50000} for _ in range(25)],
                },
                "cooldownSec": 5,
            }
            try:
                with (
                    patch.object(game_tools, "current_service", return_value=service),
                    patch.object(
                        service,
                        "submit",
                        new=AsyncMock(
                            side_effect=lambda *args: copy.deepcopy(feedback)
                        ),
                    ),
                ):
                    response = await game_tools.submit_solution(
                        "test", 1, "1", solution="answer"
                    )
                    data = response.structuredContent["data"]
                    self.assertFalse(response.isError)
                    self.assertLess(len(response.model_dump_json()), 50000)
                    self.assertEqual(data["cooldownSec"], 5)
                    evaluation = data["evaluation"]
                    self.assertEqual(evaluation["case_count"], 26)
                    self.assertEqual(evaluation["failed_count"], 25)
                    self.assertEqual(len(evaluation["failed_cases"]), 20)
                    self.assertEqual(evaluation["failed_cases"][0]["case_index"], 1)
                    self.assertTrue(evaluation["failed_cases"][0]["truncated"])
                    saved = service.artifacts.path(data["full_result"]["artifact_id"])
                    self.assertEqual(json.loads(saved.read_bytes()), feedback)
                    full = await game_tools.submit_solution(
                        "test", 1, "1", solution="answer", include_case_details=True
                    )
                    self.assertEqual(full.structuredContent["data"], feedback)
            finally:
                await client.close()

    async def test_unfamiliar_submission_feedback_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            feedback = None

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
                    return httpx.Response(200, json={"token": "test-token"})
                return httpx.Response(200, json=feedback)

            client = CCCClient(
                Settings(data_dir=Path(root), cookie="XSRF-TOKEN=csrf"),
                httpx.MockTransport(handler),
            )
            service = Service(client)
            try:
                with patch.object(game_tools, "current_service", return_value=service):
                    for feedback in (
                        {"evaluation": None},
                        {"evaluation": {"cases": ["custom result"]}},
                        {"evaluation": {"cases": {"first": True}}},
                        ["custom result"],
                    ):
                        with self.subTest(feedback=feedback):
                            response = await game_tools.submit_solution(
                                "test", 1, "1", solution="answer"
                            )
                            self.assertFalse(response.isError)
                            self.assertEqual(
                                response.structuredContent["data"], feedback
                            )
            finally:
                await client.close()

    async def test_large_upload_and_raw_writes_use_request_settings(self):
        with tempfile.TemporaryDirectory() as root:
            client = CCCClient(
                Settings(
                    data_dir=Path(root),
                    max_bytes=2 * 1024 * 1024,
                    cookie="XSRF-TOKEN=csrf",
                    enable_raw_writes=True,
                ),
                httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"accepted": True})
                ),
            )
            service = Service(client)
            try:
                with patch.object(game_tools, "current_service", return_value=service):
                    payload = b"x" * (1024 * 1024 + 1)
                    uploaded = await game_tools.upload_artifact(
                        base64.b64encode(payload).decode()
                    )
                    self.assertFalse(uploaded.isError)
                    self.assertEqual(
                        uploaded.structuredContent["data"]["bytes"], len(payload)
                    )
                    oversized = await game_tools.upload_artifact(
                        base64.b64encode(
                            b"x" * (client.settings.max_bytes + 1)
                        ).decode()
                    )
                    self.assertTrue(oversized.isError)
                    response = await game_tools.ccc_api_request(
                        "POST", "/api/custom", body={"value": 1}
                    )
                    self.assertFalse(response.isError)
            finally:
                await client.close()

    async def test_report_storage_failure_preserves_submission_result(self):
        with tempfile.TemporaryDirectory() as root:
            client = CCCClient(
                Settings(data_dir=Path(root)),
                httpx.MockTransport(lambda _: self.fail("Unexpected request")),
            )
            service = Service(client)
            feedback = {
                "evaluation": {"isCorrect": True, "cases": [{"isCorrect": True}]},
                "cooldownSec": 4,
            }
            try:
                with (
                    patch.object(game_tools, "current_service", return_value=service),
                    patch.object(
                        service, "submit", new=AsyncMock(return_value=feedback)
                    ) as submit,
                    patch.object(
                        service.artifacts, "save", side_effect=OSError("Disk full")
                    ),
                ):
                    response = await game_tools.submit_solution(
                        "test", 1, "1-small", solution="answer"
                    )
                submit.assert_awaited_once()
                self.assertFalse(response.isError)
                self.assertTrue(
                    response.structuredContent["data"]["evaluation"]["isCorrect"]
                )
                self.assertEqual(
                    response.structuredContent["data"]["evaluation"]["cases"],
                    [{"isCorrect": True}],
                )
                self.assertIn("storage_warning", response.structuredContent["data"])
            finally:
                await client.close()

    async def test_file_fallback_locked_level_and_compact_feedback(self):
        with tempfile.TemporaryDirectory() as root:
            submissions = []

            def handler(request):
                path = request.url.path
                if path == "/api/contests/test":
                    data = {
                        "slug": "test",
                        "durationMinutes": 120,
                        "gameBaseUrl": "https://birds.codingcontest.org",
                    }
                elif path == "/api/game-token":
                    data = {"token": "test-token"}
                elif path == "/game/game-info":
                    data = {
                        "levelsInfo": {
                            "levels": [
                                {"inputFiles": ["1-small"]},
                                {"inputFiles": ["1-small"]},
                            ]
                        }
                    }
                elif path == "/api/contestant/contestant-info":
                    data = {"score": {"gameScore": {"level": 1}}}
                elif path.endswith("/files"):
                    if request.url.params.get("raw") == "true":
                        return httpx.Response(
                            200,
                            content=b"archive-bytes",
                            headers={"content-type": "application/zip"},
                        )
                    data = {"url": "https://cdn.example.com/signed-secret"}
                elif path == "/api/contestant/submit-1-1-small":
                    submissions.append(request)
                    data = {
                        "evaluation": {
                            "isCorrect": False,
                            "cases": [{"isCorrect": False}],
                        },
                        "cooldownSec": 4,
                    }
                elif path == "/api/contestant/submit-2-1-small":
                    return httpx.Response(403, json={"error": "Level locked by CCC"})
                else:
                    self.fail("Unexpected upstream request")
                return httpx.Response(200, json=data)

            client = CCCClient(
                Settings(cookie="XSRF-TOKEN=csrf", data_dir=Path(root)),
                httpx.MockTransport(handler),
            )
            service = Service(client)
            try:
                artifact = await service.asset(
                    "test", "/api/contestant/level/1/files", "level.zip"
                )
                self.assertEqual(
                    service.artifacts.path(artifact["artifact_id"]).read_bytes(),
                    b"archive-bytes",
                )
                with self.assertRaises(APIError) as rejected:
                    await service.submit("test", 2, "1-small", b"42", "answer.out")
                self.assertEqual(rejected.exception.status, 403)
                self.assertEqual(submissions, [])
                with patch.object(game_tools, "current_service", return_value=service):
                    result = await game_tools.submit_solution(
                        "test", 1, "1-small", solution="42"
                    )
                self.assertFalse(result.isError)
                data = result.structuredContent["data"]
                self.assertFalse(data["evaluation"]["isCorrect"])
                self.assertEqual(data["evaluation"]["failed_count"], 1)
                self.assertNotIn("cases", data["evaluation"])
                self.assertTrue(
                    service.artifacts.path(data["full_result"]["artifact_id"]).is_file()
                )
            finally:
                await client.close()

    async def test_competition_resume_and_fresh_level_progress(self):
        with tempfile.TemporaryDirectory() as root:
            level = 1
            tokens = 0

            def handler(request):
                nonlocal level, tokens
                path = request.url.path
                if path == "/api/contests/competition":
                    # Competitions need neither a mode nor a training endpoint.
                    data = {
                        "slug": "competition",
                        "gameBaseUrl": "https://birds.codingcontest.org",
                    }
                elif path == "/api/game-token":
                    tokens += 1
                    data = {"token": "game-token"}
                elif path == "/game/game-info":
                    data = {"levelsInfo": {"levels": [{"inputFiles": ["1"]}] * 2}}
                elif path == "/api/contestant/contestant-info":
                    data = {"score": {"gameScore": {"level": level}}}
                elif path == "/api/contestant/submit-1-1":
                    level = 2
                    data = {"evaluation": {"isCorrect": True}}
                elif path == "/api/contestant/level/2/input/1":
                    if level < 2:
                        return httpx.Response(403, json={"error": "Locked"})
                    return httpx.Response(200, content=b"next level input")
                else:
                    self.fail(f"Unexpected request: {path}")
                return httpx.Response(200, json=data)

            client = CCCClient(
                Settings(cookie="XSRF-TOKEN=csrf", data_dir=Path(root)),
                httpx.MockTransport(handler),
            )
            try:
                service = Service(client)
                info = await service.info(
                    "https://codingcontest.org/contests/competition/game"
                )
                self.assertIsNone(info["levels"][1]["accessible"])
                with self.assertRaises(APIError):
                    await service.asset(
                        "competition", "/api/contestant/level/2/input/1", "input.txt"
                    )
                await service.submit(
                    info["resume"]["contest"], 1, "1", b"answer", "answer.out"
                )
                resumed = Service(client, service.games)
                self.assertTrue(
                    (await resumed.info(info["resume"]["url"]))["levels"][1][
                        "accessible"
                    ]
                )
                self.assertEqual(
                    (
                        await resumed.asset(
                            "competition",
                            "/api/contestant/level/2/input/1",
                            "input.txt",
                        )
                    )["bytes"],
                    16,
                )
                self.assertEqual(tokens, 1)
            finally:
                await client.close()
