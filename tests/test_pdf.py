import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from ccc_mcp import game_tools
from ccc_mcp.client import CCCClient
from ccc_mcp.config import Settings
from ccc_mcp.service import Service


class PDFTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_only_pdf_returns_native_mcp_image(self):
        with tempfile.TemporaryDirectory() as root:
            client = CCCClient(
                Settings(data_dir=Path(root)),
                httpx.MockTransport(lambda _: self.fail("Unexpected request")),
            )
            service = Service(client)
            try:
                pdf = io.BytesIO()
                with Image.new("RGB", (72, 72), color="red") as source:
                    source.save(pdf, format="PDF", resolution=72)
                artifact = service.artifacts.save(pdf.getvalue(), "statement.pdf")[
                    "artifact_id"
                ]
                self.assertTrue(service.artifacts.pdf_text(artifact)["needs_ocr"])
                with patch.object(game_tools, "current_service", return_value=service):
                    result = await game_tools.render_pdf_page(artifact, dpi=144)
                    invalid_page = await game_tools.render_pdf_page(artifact, page=1)
                    invalid_file = await game_tools.render_pdf_page("a" * 32)
                self.assertFalse(result.isError)
                self.assertTrue(invalid_page.isError)
                self.assertTrue(invalid_file.isError)
                images = [item for item in result.content if item.type == "image"]
                self.assertEqual(len(images), 1)
                with Image.open(
                    io.BytesIO(base64.b64decode(images[0].data))
                ) as rendered:
                    self.assertEqual(rendered.size, (144, 144))
                    red, green, blue = rendered.convert("RGB").getpixel((72, 72))
                    self.assertGreater(red, 240)
                    self.assertLess(green + blue, 10)
            finally:
                await client.close()
