import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from ccc_mcp.app import create_app
from ccc_mcp.artifacts import Artifacts, StorageBudget, StorageFull
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_40_clients_share_concurrency_and_disk_limits(self):
        active, peak = 0, 0

        def factory(settings):
            async def handle(request):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    return httpx.Response(200, json={"uuid": settings.session})
                finally:
                    active -= 1

            return CCCClient(settings, httpx.MockTransport(handle))

        with tempfile.TemporaryDirectory() as root:
            app = create_app(
                Settings(
                    data_dir=Path(root),
                    max_bytes=100,
                    storage_max_bytes=500,
                    max_concurrent_requests=4,
                ),
                factory,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as http:
                responses = await asyncio.gather(
                    *[
                        http.post(
                            "/mcp/artifacts",
                            content=b"x" * 100,
                            headers={"X-CCC-Session": f"{i:032x}"},
                        )
                        for i in range(40)
                    ]
                )
                self.assertEqual(sum(r.status_code == 200 for r in responses), 5)
                self.assertEqual(sum(r.status_code == 507 for r in responses), 35)
                self.assertLessEqual(peak, 4)
                self.assertEqual(app.storage.used, 500)
                self.assertEqual(
                    sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file()),
                    500,
                )
                self.assertFalse(list(Path(root).rglob("*.part")))
                self.assertEqual(app.slots._value, 4)

    async def test_large_mcp_body_is_rejected_before_authentication(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(
                Settings(data_dir=Path(root)),
                lambda _: self.fail("Unexpected upstream request"),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as http:
                response = await http.post("/mcp", content=b"x" * (2 * 1024 * 1024 + 1))
                self.assertEqual(response.status_code, 413)
                self.assertIn("binary HTTP", response.json()["error"])

    async def test_storage_reservations_rollback_and_expiration_frees_space(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            budget = StorageBudget(root, 10)
            a = Artifacts(
                root / "accounts" / ("a" * 64), 20, ttl_seconds=60, budget=budget
            )
            b = Artifacts(
                root / "accounts" / ("b" * 64), 20, ttl_seconds=60, budget=budget
            )
            first = a.save(b"123456", "first")
            with self.assertRaises(StorageFull):
                b.save_chunks([b"12", b"345"], "too-much")
            self.assertEqual(budget.used, 6)
            self.assertFalse(list(b.root.iterdir()))
            old = time.time() - 120
            os.utime(a.root / first["artifact_id"], (old, old))
            self.assertEqual(budget.cleanup(60), 1)
            self.assertEqual(budget.used, 0)
            b.save(b"0123456789", "fits")
            restarted = StorageBudget(root, 10)
            fresh = Artifacts(a.root, 20, budget=restarted)
            with self.assertRaises(StorageFull):
                fresh.save(b"x", "full")
            self.assertEqual(restarted.used, 10)

    async def test_cleanup_does_not_delete_active_partial_upload(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            budget = StorageBudget(root, 100)
            artifacts = Artifacts(root / "accounts" / ("a" * 64), 100, budget=budget)

            async def chunks():
                yield b"first"
                partial = next(artifacts.root.glob("*.part"))
                os.utime(partial, (0, 0))
                self.assertEqual(budget.cleanup(60), 0)
                self.assertTrue(partial.exists())
                yield b"second"

            result = await artifacts.receive(chunks(), "answer")
            self.assertEqual(result["bytes"], 11)
            self.assertEqual(budget.used, 11)
