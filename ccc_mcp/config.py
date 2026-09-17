import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    cookie: str = field(default="", repr=False)
    session: str = field(default="", repr=False)
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8000
    timeout: float = 30
    max_bytes: int = 64 * 1024 * 1024
    public_origin: str = "http://localhost:8000"
    enable_raw_writes: bool = False

    def __post_init__(self):
        if not 1 <= self.port <= 65535 or self.timeout <= 0 or self.max_bytes <= 0:
            raise ValueError("Invalid port, timeout or file size limit")
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
            raise ValueError(
                "MCP_PUBLIC_ORIGIN must be an HTTP(S) origin without a path"
            )

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        return cls(
            data_dir=Path(os.getenv("CCC_DATA_DIR", "./data")).resolve(),
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8000")),
            timeout=float(os.getenv("CCC_TIMEOUT", "30")),
            max_bytes=int(os.getenv("CCC_MAX_FILE_BYTES", str(64 * 1024 * 1024))),
            public_origin=os.getenv("MCP_PUBLIC_ORIGIN", "http://localhost:8000"),
            enable_raw_writes=os.getenv("CCC_ENABLE_RAW_WRITES") == "1",
        )
