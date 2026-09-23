"""Bounded artifact storage and archive inspection without extracting ZIP paths."""

import asyncio
import base64
import hashlib
import io
import math
import re
import threading
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader

# PDFium is not thread-safe, including when rendering separate documents.
_pdf_lock = threading.Lock()


class Artifacts:
    def __init__(
        self, root: Path, limit: int, public_origin: str = "http://localhost:8000"
    ):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "inbox").mkdir(exist_ok=True)
        self.limit = limit
        self.transfer_url = public_origin.rstrip("/") + "/mcp/artifacts"

    def metadata(self, artifact, filename, size, digest):
        result = dict(
            artifact_id=artifact,
            filename=filename,
            bytes=size,
            sha256=digest,
            download_url=f"{self.transfer_url}/{artifact}",
        )
        if filename.lower().endswith(".pdf"):
            try:
                result["total_pages"] = self.pdf_pages(artifact)
            except (ValueError, pdfium.PdfiumError):
                pass
        return result

    async def receive(self, chunks, filename):
        artifact = uuid.uuid4().hex
        temporary = self.root / (artifact + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as stream:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.limit:
                        raise ValueError("File exceeds CCC_MAX_FILE_BYTES")
                    await asyncio.to_thread(stream.write, chunk)
                    digest.update(chunk)
            temporary.replace(self.root / artifact)
            return await asyncio.to_thread(
                self.metadata, artifact, filename, size, digest.hexdigest()
            )
        finally:
            temporary.unlink(missing_ok=True)

    def import_file(self, relative_path: str):
        """Import a file from the explicitly shared inbox, never arbitrary server paths."""
        inbox = self.root / "inbox"
        path = (inbox / relative_path).resolve()
        if Path(relative_path).is_absolute() or not path.is_relative_to(inbox):
            raise ValueError("File must be inside CCC_DATA_DIR/inbox")
        with path.open("rb") as stream:
            return self.save(stream.read(self.limit + 1), path.name)

    def path(self, artifact: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", artifact):
            raise ValueError("Use an artifact_id returned by a download or upload tool")
        path = self.root / artifact
        if path.is_symlink() or not path.is_file():
            raise ValueError("Artifact not found")
        return path

    def save(self, data: bytes, filename: str):
        if len(data) > self.limit:
            raise ValueError("File exceeds CCC_MAX_FILE_BYTES")
        artifact = uuid.uuid4().hex
        path = self.root / artifact
        with path.open("xb") as stream:
            stream.write(data)
        return self.metadata(
            artifact, filename, len(data), hashlib.sha256(data).hexdigest()
        )

    def read(self, artifact: str, offset=0, length=8192, encoding="text"):
        if offset < 0 or not 1 <= length <= 262144:
            raise ValueError("offset >= 0 and 1 <= length <= 262144 required")
        path = self.path(artifact)
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(length)
        total = path.stat().st_size
        if encoding not in ("text", "base64"):
            raise ValueError("encoding must be text or base64")
        return dict(
            data=base64.b64encode(data).decode()
            if encoding == "base64"
            else data.decode("utf-8", errors="replace"),
            offset=offset,
            bytes=len(data),
            total_bytes=total,
            next_offset=offset + len(data) if offset + len(data) < total else None,
            download_url=f"{self.transfer_url}/{artifact}",
        )

    def archive(self, artifact: str):
        with zipfile.ZipFile(self.path(artifact)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                raise ValueError("Archive has too many entries")
            return [
                dict(
                    name=e.filename,
                    bytes=e.file_size,
                    compressed_bytes=e.compress_size,
                    directory=e.is_dir(),
                )
                for e in entries
            ]

    def member(self, artifact: str, name: str):
        with zipfile.ZipFile(self.path(artifact)) as archive:
            matches = [e for e in archive.infolist() if e.filename == name]
            if len(matches) != 1:
                raise ValueError("ZIP member missing or ambiguous")
            entry = matches[0]
            if entry.is_dir() or entry.file_size > self.limit:
                raise ValueError("ZIP member is a directory or too large")
            # Store by opaque id; archive paths are never used as filesystem paths.
            with archive.open(entry) as stream:
                payload = stream.read(self.limit + 1)
            return self.save(payload, name)

    def unpack(self, artifact: str):
        entries = self.archive(artifact)
        files = [entry for entry in entries if not entry["directory"]]
        if len(files) > 100 or sum(entry["bytes"] for entry in files) > self.limit:
            return {
                "extracted": False,
                "entries": entries,
                "hint": "Use archive_member or download the ZIP locally; automatic extraction limit exceeded",
            }
        if len({entry["name"] for entry in files}) != len(files):
            raise ValueError("ZIP contains duplicate filenames")
        return {
            "extracted": True,
            "entries": [self.member(artifact, entry["name"]) for entry in files],
        }

    def pdf_text(self, artifact: str, page: int = 0):
        if page < 0:
            raise ValueError("page must be nonnegative")
        reader = PdfReader(io.BytesIO(self.path(artifact).read_bytes()))
        if page >= len(reader.pages):
            raise ValueError("PDF page out of range")
        text = reader.pages[page].extract_text() or ""
        return dict(
            page=page,
            total_pages=len(reader.pages),
            text=text[:100000],
            truncated=len(text) > 100000,
            text_layer_empty=not text.strip(),
        )

    def pdf_image(self, artifact: str, page: int = 0, dpi: int = 120):
        if page < 0 or not 36 <= dpi <= 200:
            raise ValueError("page >= 0 and 36 <= dpi <= 200 required")
        try:
            with _pdf_lock, pdfium.PdfDocument(self.path(artifact)) as document:
                if page >= len(document):
                    raise ValueError(
                        f"PDF has {len(document)} pages; use page 0..{len(document) - 1}"
                    )
                with closing(document[page]) as source:
                    width, height = source.get_size()
                    scale = dpi / 72
                    if math.ceil(width * scale) * math.ceil(height * scale) > 8_000_000:
                        raise ValueError(
                            "Rendered page exceeds 8 megapixels; use a lower dpi"
                        )
                    with closing(source.render(scale=scale)) as bitmap:
                        with bitmap.to_pil() as image:
                            output = io.BytesIO()
                            image.save(output, format="PNG")
                            dimensions = image.size
                payload = output.getvalue()
                if len(payload) > self.limit:
                    raise ValueError("Rendered image exceeds CCC_MAX_FILE_BYTES")
                return payload, dict(
                    page=page,
                    total_pages=len(document),
                    width=dimensions[0],
                    height=dimensions[1],
                )
        except pdfium.PdfiumError as error:
            raise ValueError("PDF could not be rendered") from error

    def pdf_pages(self, artifact):
        try:
            with _pdf_lock, pdfium.PdfDocument(self.path(artifact)) as document:
                return len(document)
        except pdfium.PdfiumError as error:
            raise ValueError("PDF could not be opened") from error
