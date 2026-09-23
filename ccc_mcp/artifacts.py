"""Account-scoped streaming file storage with bounded retention."""

import asyncio
import hashlib
import re
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import anyio


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
