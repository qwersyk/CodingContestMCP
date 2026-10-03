"""CCC authentication and JSON discovery; file traffic goes directly to CCC."""

import asyncio
import re
from urllib.parse import quote, unquote, urlsplit

import httpx

PLATFORM = "https://codingcontest.org"


class APIError(RuntimeError):
    def __init__(self, status, detail, retry_after=None):
        self.status, self.detail, self.retry_after = status, detail, retry_after
        super().__init__(f"CCC HTTP {status}")


def contest_slug(value):
    if "://" in value:
        url = urlsplit(value)
        parts = url.path.strip("/").split("/")
        if (
            url.scheme != "https"
            or url.hostname not in ("codingcontest.org", "www.codingcontest.org")
            or url.username
            or url.password
            or url.port not in (None, 443)
            or len(parts) not in (2, 3)
            or parts[0] != "contests"
            or len(parts) == 3
            and parts[2] != "game"
        ):
            raise ValueError(
                "Use a started contest's slug or codingcontest.org/contests/ URL"
            )
        value = unquote(parts[1])
    if not value or value in (".", "..") or any(c in value for c in "/\\\r\n"):
        raise ValueError("Invalid contest slug")
    return quote(value, safe="")


def game_origin(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or not re.fullmatch(r"[a-z0-9-]+\.codingcontest\.org", url.hostname or "")
        or url.hostname == "www.codingcontest.org"
    ):
        raise ValueError("CCC returned an invalid HTTPS game origin")
    return f"https://{url.hostname}"


def json_response(response):
    if not response.is_success:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise APIError(
            response.status_code, detail, response.headers.get("retry-after")
        )
    return response.json() if response.content else None


class CCCClient:
    def __init__(self, settings, session, transport=None):
        options = {
            "timeout": settings.timeout,
            "follow_redirects": False,
            "transport": transport,
            "limits": httpx.Limits(max_connections=None, max_keepalive_connections=20),
            "headers": {"User-Agent": "codingcontest-mcp/3.0"},
        }
        self.platform = httpx.AsyncClient(base_url=PLATFORM, **options)
        self.platform.cookies.set(
            "SESSION", session, domain="codingcontest.org", path="/"
        )
        self.games = httpx.AsyncClient(**options)

    async def close(self):
        await asyncio.gather(self.platform.aclose(), self.games.aclose())

    def csrf(self):
        return next(
            (
                c.value
                for c in self.platform.cookies.jar
                if c.name == "XSRF-TOKEN"
                and c.domain.lstrip(".") == "codingcontest.org"
            ),
            "",
        )

    async def json(self, method, path, body=None):
        if method != "GET" and not self.csrf():
            json_response(await self.platform.get("/api/games"))
        headers = {}
        if method != "GET" and (csrf := self.csrf()):
            headers["X-XSRF-TOKEN"] = unquote(csrf)
        return json_response(
            await self.platform.request(method, path, json=body, headers=headers)
        )

    async def game_json(self, origin, path, headers):
        request = self.games.build_request(
            "GET", game_origin(origin) + path, headers=headers
        )
        # Game requests never carry platform cookies, including cookies set by a game.
        request.headers.pop("cookie", None)
        return json_response(await self.games.send(request))
