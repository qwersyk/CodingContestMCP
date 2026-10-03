"""HTTP connection settings for the direct-transfer MCP server."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    timeout: float | None = None
    public_origin: str = "http://localhost:8000"

    def __post_init__(self):
        if not 1 <= self.port <= 65535 or (
            self.timeout is not None and self.timeout <= 0
        ):
            raise ValueError("Invalid port or timeout")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in ("http", "https")
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in ("", "/")
        ):
            raise ValueError("MCP_PUBLIC_ORIGIN must be an HTTP(S) origin")

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        timeout = os.getenv("CCC_TIMEOUT")
        return cls(
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8000")),
            timeout=float(timeout) if timeout else None,
            public_origin=os.getenv("MCP_PUBLIC_ORIGIN", "http://localhost:8000"),
        )
