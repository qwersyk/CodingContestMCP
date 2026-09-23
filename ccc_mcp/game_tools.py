"""Contest-scoped tools; importing this module registers them."""

import json
from typing import Any, Literal
from urllib.parse import unquote

from .context import current_service
from .service import compact_progress
from .tools import _call, _params, local, tool


@tool(read_only=False)
async def start_training(
    query: str, mode: Literal["challenge", "exploration"] = "challenge"
):
    """Start by name, slug, UUID or CCC challenge URL. Challenge starts a timer; exploration is untimed.
    Returns training.contestName; use that value as contest in subsequent tools."""
    return await _call(lambda: current_service().start_training(query, mode))


@tool()
async def active_training():
    """List existing training sessions to resume without starting another timer."""
    return await _call(
        lambda: current_service().client.json("GET", "/api/training/active")
    )


@tool(read_only=True)
async def game_info(contest: str):
    """Get fresh progress, game metadata, inputFiles IDs and a resume reference.
    Accessible levels are hints derived from progress; CCC decides actual access."""
    return await _call(lambda: current_service().info(contest))


@tool(read_only=False)
async def prepare_level(contest: str, level: int):
    """Get a level ZIP download URL, exact inputFiles IDs and HTTP upload instructions.
    Download, extract, read statements and solve locally. Does not start or submit."""

    async def run():
        service = current_service()
        service.validate_level(level)
        info = await service.info(contest)
        archive = await service.asset(
            info["contest_slug"],
            f"/api/contestant/level/{level}/files",
            f"level-{level}.zip",
        )
        return {
            "contest": info["contest_slug"],
            "level": level,
            "level_info": next(
                (entry for entry in info["levels"] if entry["level"] == level), None
            ),
            "participant": info["participant"],
            "archive": archive,
            "transfer": {
                "upload_url": service.artifacts.url(),
                "max_file_bytes": service.client.settings.max_bytes,
                "retention_seconds": service.client.settings.artifact_ttl_seconds,
                "download": 'curl --fail --output input.zip "DOWNLOAD_URL"',
                "upload": 'curl --fail --header "Content-Type: application/octet-stream" --request POST --upload-file answer.out "UPLOAD_URL"',
            },
        }

    return await _call(run)


@tool(read_only=False)
async def submit_solution(
    contest: str,
    level: int,
    file_id: str,
    artifact_id: str,
    filename: str = "solution.out",
):
    """Submit a file previously uploaded by HTTP using its artifact_id and exact inputFiles ID.
    Returns evaluation and cooldownSec; check evaluation.isCorrect, not only ok.
    Never blindly retry an uncertain submission. Full reports are available by HTTP."""

    async def run():
        payload = current_service().artifacts.path(artifact_id)
        feedback = await current_service().submit(
            contest, level, file_id, payload, filename
        )
        evaluation = feedback.get("evaluation") if isinstance(feedback, dict) else None
        cases = evaluation.get("cases") if isinstance(evaluation, dict) else None
        if (
            isinstance(cases, list)
            and cases
            and all(isinstance(case, dict) for case in cases)
        ):
            full = None
            try:
                full = await local(
                    lambda: current_service().artifacts.save(
                        json.dumps(feedback).encode(), "submission-result.json"
                    )
                )
            except (OSError, ValueError):
                feedback["storage_warning"] = "Could not save the full report."
            feedback["evaluation"].pop("cases")
            failed_count = 0
            previews = []
            for index, case in enumerate(cases):
                if case.get("isCorrect"):
                    continue
                failed_count += 1
                if len(previews) >= 20:
                    continue
                encoded = json.dumps(case, ensure_ascii=False)
                previews.append(
                    {**case, "case_index": index}
                    if len(encoded) <= 1024
                    else {
                        "case_index": index,
                        "truncated": True,
                        "preview": encoded[:1024],
                    }
                )
            feedback["evaluation"].update(
                case_count=len(cases),
                failed_count=failed_count,
                failed_cases=previews,
            )
            if full:
                feedback["full_result"] = full
        return compact_progress(feedback)

    return await _call(run)


@tool(read_only=False)
async def ccc_api_request(
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
    path: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    response_format: Literal["json", "file"] = "json",
    filename: str = "response.bin",
):
    """Platform API escape hatch. Non-GET requires CCC_ENABLE_RAW_WRITES=1.
    Use dedicated tools for ordinary operations. Account permissions still apply."""

    async def run():
        if method != "GET" and not current_service().client.settings.enable_raw_writes:
            raise ValueError("Set CCC_ENABLE_RAW_WRITES=1 for generic mutations")
        decoded = path
        for _ in range(4):
            decoded = unquote(decoded)
        if (
            decoded.split("?")[0].startswith("/api/auth/")
            and decoded.split("?")[0] != "/api/auth/current-user"
        ):
            raise ValueError("Raw authentication endpoints are disabled")
        response = await current_service().client.request(
            method,
            path,
            params=_params(query),
            json_body=body,
            download=(
                lambda chunks: current_service().artifacts.receive(chunks, filename)
            )
            if response_format == "file"
            else None,
        )
        if response_format == "file":
            return (
                response
                if isinstance(response, dict)
                else await local(
                    lambda: current_service().artifacts.save(response.content, filename)
                )
            )
        return response.json() if response.content else None

    return await _call(run)


@tool(read_only=False)
async def game_api_request(
    contest: str,
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
    path: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    response_format: Literal["json", "file"] = "json",
    filename: str = "response.bin",
):
    """Game API escape hatch scoped to contest. Non-GET requires CCC_ENABLE_RAW_WRITES=1."""

    async def run():
        if method != "GET" and not current_service().client.settings.enable_raw_writes:
            raise ValueError("Set CCC_ENABLE_RAW_WRITES=1 for generic mutations")
        response = await current_service().request(
            contest,
            method,
            path,
            params=_params(query),
            json_body=body,
            download=(
                lambda chunks: current_service().artifacts.receive(chunks, filename)
            )
            if response_format == "file"
            else None,
        )
        if response_format == "file":
            return (
                response
                if isinstance(response, dict)
                else await local(
                    lambda: current_service().artifacts.save(response.content, filename)
                )
            )
        return response.json() if response.content else None

    return await _call(run)
