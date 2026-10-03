"""Stateless MCP connection discovery; CCC handles all file and submission traffic."""

import logging

import uvicorn

from .client import CCCClient
from .config import Settings
from .tools import create_mcp


def create_app(configured=None, client_factory=CCCClient):
    return create_mcp(
        configured or Settings.from_env(), client_factory
    ).streamable_http_app()


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port, access_log=False
    )
