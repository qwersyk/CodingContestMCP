"""One connection tool; the agent handles downloads, solving and submissions."""

import asyncio
import json
import re
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .client import APIError, CCCClient, contest_slug, game_origin

INSTRUCTIONS = (
    "The user starts the contest on codingcontest.org and gives you its game URL. "
    "Call connect_contest once with that URL. It returns metadata, current progress and "
    "connection.base_url/headers for direct HTTPS requests to CCC. It does not start a timer. "
    "Download a level ZIP using http.level_files (replace {level}) and the returned headers. "
    "Extract it locally, read the PDF, and solve every scored inputFiles ID from "
    "game.levelsInfo.levels[level-1]; unscoredFiles are examples. "
    "Upload each local output DIRECTLY to http.submit (replace {level} and {file_id}), "
    "using multipart/form-data with field name solution. Do not upload files to this MCP server. "
    "Use evaluation.isCorrect and the progress endpoint to decide the next step. "
    "Requests and uploads may run concurrently; CCC owns access, cooldowns and rate limits. "
    "No local delays, file-size limits, response truncation or submission retries are added. "
    "Inspect CCC's Retry-After and cooldownSec yourself. On an expired token/401, "
    "call connect_contest again; on an uncertain upload outcome inspect progress before retrying. "
    "Send connection.headers only to the exact returned game origin, never to other hosts or "
    "signed external download URLs. The returned game token is a private credential; "
    "keep it out of shared files and messages. The platform SESSION stays in the MCP connection header."
)


def result(data, error=False):
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data,
        isError=error,
    )


def create_mcp(settings, client_factory=CCCClient):
    origin = urlsplit(settings.public_origin)
    mcp = FastMCP(
        "codingcontest",
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
        instructions=INSTRUCTIONS,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "localhost",
                "127.0.0.1",
                "[::1]",
                "localhost:*",
                "127.0.0.1:*",
                "[::1]:*",
                origin.netloc,
            ],
            allowed_origins=[
                "http://localhost:*",
                "http://127.0.0.1:*",
                settings.public_origin,
            ],
        ),
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    async def connect_contest(contest: str, ctx: Context) -> CallToolResult:
        """Connect to a contest already started by the user; accept its game URL or slug.
        Returns game metadata/inputFiles, progress, and game-scoped headers for direct HTTP.
        GET http.level_files with these headers to download the ZIP and solve it locally.
        POST each output directly to http.submit as multipart field solution; check evaluation.isCorrect.
        Replace {level}/{file_id} in the URLs. Parallel requests are allowed; CCC enforces its limits.
        Refresh an expired game token by calling this tool again. Requires X-CCC-Session in the MCP connection.
        No training is started and no files or CCC sessions are stored on this MCP server."""
        client = None
        try:
            slug = contest_slug(contest)
            request = ctx.request_context.request
            sessions = request.headers.getlist("x-ccc-session") if request else []
            if len(sessions) != 1 or not re.fullmatch(
                r"[A-Za-z0-9_+/=%.-]+", sessions[0]
            ):
                raise APIError(
                    401,
                    "Supply the SESSION cookie value in the X-CCC-Session MCP connection header",
                )
            client = client_factory(settings, sessions[0])
            data = await client.json("GET", f"/api/contests/{slug}")
            base_url = game_origin(data["gameBaseUrl"])
            # CCC authenticates the supplied session and checks contest access here.
            token_data = await client.json(
                "POST", "/api/game-token", {"contestSlug": data["slug"]}
            )
            token = token_data.get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("CCC returned no game token")
            headers = {
                "Authorization": token,
                "X-CCC-SLUG": data["slug"],
                "Referer": "https://codingcontest.org/",
            }
            state = await asyncio.gather(
                client.game_json(base_url, "/game/game-info", headers),
                client.game_json(base_url, "/api/contestant/contestant-info", headers),
                return_exceptions=True,
            )
            for value in state:
                if isinstance(value, BaseException):
                    raise value
            metadata, progress = state
            return result(
                {
                    "ok": True,
                    "contest": data["slug"],
                    "connection": {"base_url": base_url, "headers": headers},
                    "game": metadata,
                    "participant": progress,
                    "http": {
                        "metadata": base_url + "/game/game-info",
                        "progress": base_url + "/api/contestant/contestant-info",
                        "level_files": base_url
                        + "/api/contestant/level/{level}/files?raw=true",
                        "submit": base_url + "/api/contestant/submit-{level}-{file_id}",
                        "multipart_field": "solution",
                        "contest_page": "https://codingcontest.org/contests/"
                        + quote(data["slug"], safe="")
                        + "/game",
                    },
                }
            )
        except APIError as error:
            return result(
                {
                    "ok": False,
                    "error": {
                        "status": error.status,
                        "detail": error.detail,
                        "retry_after": error.retry_after,
                    },
                },
                True,
            )
        except httpx.RequestError as error:
            return result(
                {
                    "ok": False,
                    "error": {
                        "type": type(error).__name__,
                        "detail": "CCC connection failed; retry connect_contest.",
                    },
                },
                True,
            )
        except (ValueError, KeyError, TypeError) as error:
            return result(
                {
                    "ok": False,
                    "error": {"type": type(error).__name__, "detail": str(error)},
                },
                True,
            )
        finally:
            if client is not None:
                await client.close()

    return mcp
