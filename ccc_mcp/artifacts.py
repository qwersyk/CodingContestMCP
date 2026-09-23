"""Bounded artifact storage and archive inspection without extracting ZIP paths."""

import asyncio
import base64
import hashlib
import io
import math
import re
import threading
import time
import uuid
import zipfile
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

import anyio
import pypdfium2 as pdfium
from pypdf import PdfReader

# PDFium is not thread-safe, including when rendering separate documents.
_pdf_lock = threading.Lock()


class StorageFull(OSError):
    pass


def stored_files(root):
    for account in (root / "accounts").glob("*"):
        if (
            not re.fullmatch(r"[a-f0-9]{64}", account.name)
            or account.is_symlink()
            or not account.is_dir()
        ):
            continue
        for path in account.iterdir():
            if (
                re.fullmatch(r"[a-f0-9]{32}(?:\.part)?", path.name)
                and not path.is_symlink()
            ):
                yield path


class StorageBudget:
    """Single-worker disk accounting shared by every account and writer."""

    def __init__(self, root, limit):
        self.root, self.limit = root.resolve(), limit
        self.lock = threading.RLock()
        self.used = None
        self.active = set()

    def initialize(self):
        if self.used is None:
            self.used = sum(
                path.stat().st_size
                for path in stored_files(self.root)
                if path.is_file()
            )

    @contextmanager
    def writing(self, path):
        allocated = 0
        with self.lock:
            self.initialize()
            self.active.add(path)

        def claim(size):
            nonlocal allocated
            with self.lock:
                if self.used + size > self.limit:
                    raise StorageFull(
                        "Artifact storage is full; retry after expired files are cleaned up"
                    )
                self.used += size
                allocated += size

        try:
            yield claim
        except BaseException:
            with self.lock:
                path.unlink(missing_ok=True)
                self.used -= allocated
            raise
        finally:
            with self.lock:
                self.active.discard(path)

    def cleanup(self, ttl_seconds):
        with self.lock:
            self.initialize()
            return Artifacts.cleanup(self.root, ttl_seconds, self)


class Artifacts:
    def __init__(
        self,
        root: Path,
        limit: int,
        public_origin: str = "http://localhost:8000",
        ttl_seconds: int = 21600,
        budget: StorageBudget | None = None,
    ):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self.budget = budget
        self.transfer_url = public_origin.rstrip("/") + "/mcp/artifacts"

    def metadata(self, artifact, filename, size, digest):
        result = dict(
            artifact_id=artifact,
            filename=filename,
            bytes=size,
            sha256=digest,
            download_url=f"{self.transfer_url}/{artifact}",
            expires_at=int((self.root / artifact).stat().st_mtime + self.ttl_seconds),
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
            with (
                self.budget.writing(temporary)
                if self.budget
                else nullcontext(lambda size: None) as claim
            ):
                with temporary.open("xb") as stream:
                    async for chunk in chunks:
                        size += len(chunk)
                        if size > self.limit:
                            raise ValueError("File exceeds CCC_MAX_FILE_BYTES")

                        def write():
                            claim(len(chunk))
                            stream.write(chunk)

                        await anyio.to_thread.run_sync(write)
                        digest.update(chunk)
                temporary.replace(self.root / artifact)
            return await asyncio.to_thread(
                self.metadata, artifact, filename, size, digest.hexdigest()
            )
        finally:
            temporary.unlink(missing_ok=True)

    def path(self, artifact: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", artifact):
            raise ValueError("Use an artifact_id returned by a download or upload tool")
        path = self.root / artifact
        if path.is_symlink() or not path.is_file():
            raise ValueError("Artifact not found")
        if path.stat().st_mtime + self.ttl_seconds <= time.time():
            raise ValueError("Artifact expired; download or upload the file again")
        return path

    def save(self, data: bytes, filename: str):
        return self.save_chunks([data], filename)

    def save_chunks(self, chunks, filename):
        artifact = uuid.uuid4().hex
        temporary = self.root / (artifact + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            with (
                self.budget.writing(temporary)
                if self.budget
                else nullcontext(lambda size: None) as claim
            ):
                with temporary.open("xb") as stream:
                    for chunk in chunks:
                        size += len(chunk)
                        if size > self.limit:
                            raise ValueError("File exceeds CCC_MAX_FILE_BYTES")
                        claim(len(chunk))
                        stream.write(chunk)
                        digest.update(chunk)
                temporary.replace(self.root / artifact)
            return self.metadata(artifact, filename, size, digest.hexdigest())
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def cleanup(root: Path, ttl_seconds: int, budget=None):
        cutoff, removed = time.time() - ttl_seconds, 0
        for path in stored_files(root):
            if budget and path in budget.active:
                continue
            try:
                stat = path.stat()
                if path.is_file() and stat.st_mtime <= cutoff:
                    path.unlink()
                    removed += 1
                    if budget:
                        budget.used -= stat.st_size
            except FileNotFoundError:
                pass
        return removed

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
                return self.save_chunks(iter(lambda: stream.read(262144), b""), name)

    def unpack(self, artifact: str):
        entries = self.archive(artifact)
        files = [entry for entry in entries if not entry["directory"]]
        if len({entry["name"] for entry in files}) != len(files):
            raise ValueError("ZIP contains duplicate filenames")
        extraction_budget = min(self.limit, 16 * 1024 * 1024)
        if (
            len(files) > 100
            or sum(entry["bytes"] for entry in files) > extraction_budget
        ):
            statements = []
            for entry in files:
                if (
                    entry["name"].lower().endswith(".pdf")
                    and entry["bytes"] <= extraction_budget
                    and len(statements) < 6
                ):
                    statements.append(self.member(artifact, entry["name"]))
                    extraction_budget -= entry["bytes"]
            return {
                "extracted": False,
                "entries": entries[:100],
                "total_entries": len(entries),
                "statements": statements,
                "hint": "Large archive: download the ZIP and extract inputs locally. PDF statements are prepared separately when small enough.",
            }
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
