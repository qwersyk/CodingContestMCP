"""Connect once; the agent performs all game HTTP operations on its computer."""

import json
import re
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .client import APIError, CCCClient, contest_slug, game_origin

CONNECT_DESCRIPTION = (
    "Connect to a user-started CCC contest URL/slug; requires X-CCC-Session in the MCP connection. "
    "Returns base_url, headers and paths for direct HTTPS from your computer. "
    "Use all headers, including Referer, only with base_url. "
    "Save HTTP bodies to local files; print only needed fields "
    "(submit: evaluation.isCorrect and brief errors). "
    "GET info/progress: levelsInfo.levels[level-1] contains inputFiles IDs and unscoredFiles examples; "
    "score.gameScore.level is current. GET files for {level}: extract ZIP, solve locally. "
    "Read CCC PDFs visually: render pages once with an available renderer "
    "(e.g. pdftoppm -r 120 -png statement.pdf page), then view the images; "
    "zoom unclear details. Skip OCR/text extraction. "
    "POST submit for {level}/{file_id} as multipart/form-data field solution. "
    "evaluation.isCorrect reports correctness; non-null passedFiles entries are saved passes. "
    "CCC cooldowns are per file (progress.cooldowns); concurrent writes can overwrite progress. "
    "On 401 call this tool again. Keep tokens private."
)


def result(data, error=False):
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        isError=error,
    )


def create_mcp(settings, client_factory=CCCClient):
    public_origin = settings.public_origin.rstrip("/")
    mcp = FastMCP(
        "codingcontest",
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "localhost",
                "127.0.0.1",
                "[::1]",
                "localhost:*",
                "127.0.0.1:*",
                "[::1]:*",
                urlsplit(public_origin).netloc,
            ],
            allowed_origins=[
                "http://localhost:*",
                "http://127.0.0.1:*",
                public_origin,
            ],
        ),
    )

    @mcp.tool(
        description=CONNECT_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    async def connect_contest(contest: str, ctx: Context) -> CallToolResult:
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
            client = client_factory(sessions[0])
            data = await client.json("GET", f"/api/contests/{slug}")
            base_url = game_origin(data["gameBaseUrl"])
            # CCC authenticates the session and authorizes access to this contest.
            token_data = await client.json(
                "POST", "/api/game-token", {"contestSlug": data["slug"]}
            )
            token = token_data.get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("CCC returned no game token")
            return result(
                {
                    "base_url": base_url,
                    "headers": {
                        "Authorization": token,
                        "X-CCC-SLUG": data["slug"],
                        "Referer": "https://codingcontest.org/",
                    },
                    "paths": {
                        "info": "/game/game-info",
                        "progress": "/api/contestant/contestant-info",
                        "files": "/api/contestant/level/{level}/files?raw=true",
                        "submit": "/api/contestant/submit-{level}-{file_id}",
                    },
                }
            )
        except APIError as error:
            return result(
                {
                    "status": error.status,
                    "error": error.detail,
                    "retry_after": error.retry_after,
                },
                True,
            )
        except httpx.RequestError as error:
            return result(
                {
                    "type": type(error).__name__,
                    "error": "CCC connection failed; retry connect_contest.",
                },
                True,
            )
        except (ValueError, KeyError, TypeError) as error:
            return result({"type": type(error).__name__, "error": str(error)}, True)
        finally:
            if client is not None:
                await client.close()

    return mcp
