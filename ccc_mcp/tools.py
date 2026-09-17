"""MCP tools operating on the authenticated request account."""

from __future__ import annotations

import asyncio
import json
import zipfile
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pypdf.errors import PdfReadError

from .client import APIError
from .config import Settings
from .context import current_service
from .service import segment

settings = Settings.from_env()

_registered_tools = []


def create_mcp(configured: Settings):
    mcp = FastMCP(
        "codingcontest",
        host=configured.host,
        port=configured.port,
        stateless_http=True,
        json_response=True,
        instructions="Each connection uses its own CCC account. list_challenges -> start_training(query, mode) -> "
        "start_game(contest=training.contestName) -> game_info -> download_level_files. "
        "Use list_archive/archive_member/read_pdf to inspect statements; render_pdf_page displays diagrams and image-only pages. "
        "submit_solution accepts text or artifact_id. Keep contest slug; tokens stay private. "
        "Check evaluation.isCorrect, score and cooldownSec. Never blindly retry an uncertain submission. "
        "Read game_info first. Use exact inputFiles IDs. Registrations and invitations change the account. "
        "For an existing training or competition call start_game with its slug or contest URL; "
        "do not call start_training to resume. Check active_training/my_registrations to find existing games. "
        "Competition entry needs no training mode. Share resume.contest to hand off to another agent. "
        "On 429 wait retry_after seconds; concurrent calls share the token cooldown.",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "localhost",
                "127.0.0.1",
                "[::1]",
                "127.0.0.1:*",
                "localhost:*",
                "[::1]:*",
                urlsplit(configured.public_origin).netloc,
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                configured.public_origin,
            ],
        ),
    )
    for fn, hints in _registered_tools:
        mcp.tool(annotations=hints, structured_output=False)(fn)
    return mcp


def tool(read_only=None):
    def register(fn):
        read = (
            read_only
            if read_only is not None
            else fn.__name__.startswith(
                (
                    "get_",
                    "list_",
                    "my_",
                    "auth_",
                    "check_",
                    "hall_",
                    "team_invitations",
                    "active_",
                )
            )
        )
        if fn.__name__ == "get_or_create_private_team":
            read = False
        annotations = ToolAnnotations(
            readOnlyHint=read,
            destructiveHint=not read,
            idempotentHint=read,
            openWorldHint=True,
        )
        _registered_tools.append((fn, annotations))
        return fn

    return register


def result(data, error=False):
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data,
        isError=error,
    )


def _params(values):
    return {k: v for k, v in (values or {}).items() if v is not None}


async def _call(fn):
    try:
        data = await fn()
        return (
            data
            if isinstance(data, CallToolResult)
            else result({"ok": True, "data": data})
        )
    except APIError as error:
        hint = {
            401: "Update the X-CCC-Session header in your MCP connection.",
            403: "Check participant role, registration, contest state and CSRF.",
            429: "Respect retry_after/cooldown before retrying.",
            413: "Solution exceeds upstream file limit.",
        }.get(error.status, "Inspect current contest state.")
        return result(
            {
                "ok": False,
                "error": {
                    "status": error.status,
                    "detail": error.detail,
                    "retry_after": error.retry_after,
                    "hint": hint,
                },
            },
            True,
        )
    except httpx.RequestError:
        return result(
            {
                "ok": False,
                "error": {
                    "type": "network_error",
                    "hint": "Mutation outcome may be unknown. Inspect game_info before retrying.",
                },
            },
            True,
        )
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, PdfReadError) as error:
        return result(
            {
                "ok": False,
                "error": {"type": type(error).__name__, "detail": str(error)},
            },
            True,
        )


async def local(fn):
    return await asyncio.to_thread(fn)


@tool()
async def auth_status() -> CallToolResult:
    """Return the signed-in CCC user, or an authentication error if no session is configured."""

    return await _call(
        lambda: current_service().client.json("GET", "/api/auth/current-user")
    )


@tool()
async def update_profile(profile: dict[str, Any]) -> CallToolResult:
    """Update the signed-in user's profile fields accepted by CCC."""

    if not profile:
        return result(
            {
                "ok": False,
                "error": {"type": "validation", "detail": "profile cannot be empty"},
            },
            True,
        )
    return await _call(
        lambda: current_service().client.json(
            "PUT", "/api/auth/current-user", json_body=profile
        )
    )


@tool()
async def check_username_availability(username: str) -> CallToolResult:
    """Check whether a username is available without changing the account."""

    return await _call(
        lambda: current_service().client.json(
            "GET", "/api/auth/check-username", params={"username": username}
        )
    )


@tool()
async def check_email_availability(email: str) -> CallToolResult:
    """Check whether an email is available without changing the account."""

    return await _call(
        lambda: current_service().client.json(
            "GET", "/api/auth/check-email", params={"email": email}
        )
    )


@tool()
async def accept_participant_role() -> CallToolResult:
    """Enable the Participant role required to start training and join contests."""

    return await _call(
        lambda: current_service().client.json(
            "POST", "/api/auth/accept-participant-role", json_body=None
        )
    )


@tool()
async def list_challenges() -> CallToolResult:
    """List all public training games. Each item includes its slug, name, description and level count."""

    return await _call(lambda: current_service().client.json("GET", "/api/games"))


@tool()
async def get_challenge(slug: str) -> CallToolResult:
    """Get a challenge definition and progress metadata by its game slug."""

    return await _call(
        lambda: current_service().client.json("GET", f"/api/training/{segment(slug)}")
    )


@tool()
async def list_contests(
    page: int = 0,
    size: int = 20,
    statuses: list[str] | None = None,
    search: str | None = None,
) -> CallToolResult:
    """Search contests and competitions. Statuses may include UPCOMING, OPEN_REGISTRATION, RUNNING, STATS_FROZEN and FINISHED."""

    if page < 0 or size < 1 or size > 100:
        return result(
            {
                "ok": False,
                "error": {
                    "type": "validation",
                    "detail": "page >= 0 and 1 <= size <= 100 required",
                },
            },
            True,
        )
    query: dict[str, Any] = {"page": page, "size": size, "search": search}
    if statuses:
        query["status"] = statuses
    return await _call(
        lambda: current_service().client.json(
            "GET", "/api/contests", params=_params(query)
        )
    )


@tool()
async def get_contest(contest: str) -> CallToolResult:
    """Get a contest by slug, including status, game URL, venues and team limits."""

    return await _call(
        lambda: current_service().client.json(
            "GET", f"/api/contests/{segment(contest)}"
        )
    )


@tool()
async def get_active_edition() -> CallToolResult:
    """Return the currently active contest edition, if one exists."""

    return await _call(
        lambda: current_service().client.json("GET", "/api/editions/active")
    )


@tool()
async def list_venues(query: dict[str, Any] | None = None) -> CallToolResult:
    """Search contest venues; query accepts the same filters as the website's /api/venues endpoint."""

    return await _call(
        lambda: current_service().client.json(
            "GET", "/api/venues", params=_params(query)
        )
    )


@tool()
async def discover_venues(contest_ids: list[int]) -> CallToolResult:
    """Return venue availability for several contest ids."""

    if not contest_ids:
        return result(
            {
                "ok": False,
                "error": {
                    "type": "validation",
                    "detail": "contest_ids cannot be empty",
                },
            },
            True,
        )
    return await _call(
        lambda: current_service().client.json(
            "GET",
            "/api/venues/discovery",
            params={"contestIds": [str(x) for x in contest_ids]},
        )
    )


@tool()
async def my_registrations() -> CallToolResult:
    """List the current user's contest registrations and team/venue choices."""

    return await _call(
        lambda: current_service().client.json("GET", "/api/contests/my-registrations")
    )


@tool()
async def register_for_contest(
    contest: str, team_id: int | None = None, venue_id: int | None = None
) -> CallToolResult:
    """Register for a contest. A team_id and/or venue_id can be supplied when the contest requires them."""

    body = _params({"teamId": team_id, "venueId": venue_id})
    return await _call(
        lambda: current_service().client.json(
            "POST", f"/api/contests/{segment(contest)}/register", json_body=body
        )
    )


@tool()
async def register_for_contests(
    contests: list[str], team_id: int | None = None, venue_id: int | None = None
) -> CallToolResult:
    """Register for several contests in one request."""

    if not contests:
        return result(
            {
                "ok": False,
                "error": {"type": "validation", "detail": "contests cannot be empty"},
            },
            True,
        )
    body = {
        "contestNames": contests,
        **_params({"teamId": team_id, "venueId": venue_id}),
    }
    return await _call(
        lambda: current_service().client.json(
            "POST", "/api/contests/registrations", json_body=body
        )
    )


@tool()
async def unregister_from_contest(contest: str) -> CallToolResult:
    """Remove the current user's registration from a contest."""

    return await _call(
        lambda: current_service().client.json(
            "DELETE", f"/api/contests/{segment(contest)}/register"
        )
    )


@tool()
async def change_registration_venue(contest: str, venue_id: int) -> CallToolResult:
    """Change the venue for an existing registration."""

    return await _call(
        lambda: current_service().client.json(
            "PUT",
            f"/api/contests/{segment(contest)}/register",
            json_body={"venueId": venue_id},
        )
    )


@tool()
async def my_results(page: int = 0, size: int = 20) -> CallToolResult:
    """List the current user's contest results."""

    return await _call(
        lambda: current_service().client.json(
            "GET", "/api/contests/my-results", params={"page": page, "size": size}
        )
    )


@tool()
async def my_scores() -> CallToolResult:
    """List training and contest scores visible to the current user."""

    return await _call(
        lambda: current_service().client.json("GET", "/api/contests/my-scores")
    )


@tool()
async def my_certificates() -> CallToolResult:
    """List certificates earned by the current user."""

    return await _call(
        lambda: current_service().client.json("GET", "/api/certificates")
    )


@tool()
async def hall_of_fame_summary(edition: str | None = None) -> CallToolResult:
    """Return the latest or a selected edition's public Hall of Fame summary."""

    path = (
        "/api/hall-of-fame/editions/latest/summary"
        if edition is None
        else f"/api/hall-of-fame/editions/{quote(edition, safe='')}/summary"
    )
    return await _call(lambda: current_service().client.json("GET", path))


@tool()
async def hall_of_fame_leaderboard(
    contest: str,
    page: int = 0,
    size: int = 100,
    search: str | None = None,
    country: str | None = None,
    venue_id: int | None = None,
) -> CallToolResult:
    """Read a public contest leaderboard with optional search, country and venue filters."""

    query = _params(
        {
            "page": page,
            "size": size,
            "search": search,
            "country": country,
            "venueId": venue_id,
        }
    )
    return await _call(
        lambda: current_service().client.json(
            "GET",
            f"/api/hall-of-fame/contests/{segment(contest)}/leaderboard",
            params=query,
        )
    )


@tool()
async def create_team(name: str) -> CallToolResult:
    """Create a team with the supplied name."""

    body = {"name": name}
    return await _call(
        lambda: current_service().client.json("POST", "/api/teams", json_body=body)
    )


@tool()
async def get_or_create_private_team() -> CallToolResult:
    """Get the user's private solo team, creating it when necessary."""

    return await _call(
        lambda: current_service().client.json(
            "POST", "/api/teams/private", json_body=None
        )
    )


@tool()
async def get_team(team_id: int) -> CallToolResult:
    """Get a team and its members."""

    return await _call(
        lambda: current_service().client.json("GET", f"/api/teams/{team_id}")
    )


@tool()
async def invite_team_member(team_id: int, email: str) -> CallToolResult:
    """Invite a member to a team by email."""

    return await _call(
        lambda: current_service().client.json(
            "POST", f"/api/teams/{team_id}/invite", json_body={"email": email}
        )
    )


@tool()
async def rename_team(team_id: int, name: str) -> CallToolResult:
    """Rename a team."""

    return await _call(
        lambda: current_service().client.json(
            "PUT", f"/api/teams/{team_id}/name", json_body={"name": name}
        )
    )


@tool()
async def leave_team(team_id: int) -> CallToolResult:
    """Leave a team."""

    return await _call(
        lambda: current_service().client.json(
            "POST", f"/api/teams/{team_id}/leave", json_body=None
        )
    )


@tool()
async def transfer_team_ownership(team_id: int, user_id: int) -> CallToolResult:
    """Transfer team ownership to another member."""

    return await _call(
        lambda: current_service().client.json(
            "PUT", f"/api/teams/{team_id}/owner", json_body={"userId": user_id}
        )
    )


@tool()
async def remove_team_member(team_id: int, user_id: int) -> CallToolResult:
    """Remove a member from a team."""

    return await _call(
        lambda: current_service().client.json(
            "DELETE", f"/api/teams/{team_id}/members/{user_id}"
        )
    )


@tool()
async def team_invitations(team_id: int) -> CallToolResult:
    """List pending invitations for a team."""

    return await _call(
        lambda: current_service().client.json(
            "GET", f"/api/teams/{team_id}/invitations"
        )
    )


@tool()
async def get_team_invitation(token: str) -> CallToolResult:
    """Inspect a team invitation token."""

    return await _call(
        lambda: current_service().client.json(
            "GET", f"/api/teams/invitations/{quote(token, safe='')}"
        )
    )


@tool()
async def accept_team_invitation(token: str) -> CallToolResult:
    """Accept a team invitation token."""

    return await _call(
        lambda: current_service().client.json(
            "POST",
            f"/api/teams/invitations/{quote(token, safe='')}/accept",
            json_body=None,
        )
    )
