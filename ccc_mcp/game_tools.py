"""Contest-scoped tools; importing this module registers them."""

import base64
import json
from typing import Any, Literal
from urllib.parse import unquote

from mcp.types import ImageContent

from .context import current_service
from .service import segment
from .tools import _call, _params, local, result, tool


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


@tool(read_only=False)
async def start_game(contest: str):
    """Open/resume an existing training or competition by slug or CCC contest/game URL.
    No training mode is needed. Does not create a new training. Share resume.contest with another agent."""
    return await _call(lambda: current_service().info(contest))


@tool(read_only=True)
async def game_info(contest: str):
    """Get fresh progress, game metadata, inputFiles IDs and a resume reference.
    Accessible levels are hints derived from progress; CCC decides actual access."""
    return await _call(lambda: current_service().info(contest))


@tool(read_only=False)
async def download_level_files(contest: str, level: int):
    """Download accessible level ZIP to an artifact. Inspect using list_archive/archive_member."""

    async def run():
        current_service().validate_level(level)
        return await current_service().asset(
            contest, f"/api/contestant/level/{level}/files", f"level-{level}.zip"
        )

    return await _call(run)


@tool(read_only=False)
async def get_level_input(contest: str, level: int, file_id: str):
    """Download one input without truncation. Read artifact contents using offsets."""

    async def run():
        current_service().validate_level(level, file_id)
        return await current_service().asset(
            contest,
            f"/api/contestant/level/{level}/input/{segment(file_id)}",
            f"level-{level}-{file_id}.in",
        )

    return await _call(run)


@tool(read_only=False)
async def get_level_sandbox(contest: str, level: int):
    """Download optional sandbox HTML as an artifact; the server does not execute it."""

    async def run():
        current_service().validate_level(level)
        return await current_service().asset(
            contest, f"/api/contestant/level/{level}/sandbox", "sandbox.html"
        )

    return await _call(run)


@tool(read_only=True)
async def read_artifact(
    artifact_id: str,
    offset: int = 0,
    length: int = 65536,
    encoding: Literal["text", "base64"] = "text",
):
    """Read a bounded byte range. Follow next_offset until null. Base64 preserves arbitrary bytes."""
    return await _call(
        lambda: local(
            lambda: current_service().artifacts.read(
                artifact_id, offset, length, encoding
            )
        )
    )


@tool(read_only=True)
async def list_archive(artifact_id: str):
    """List exact filenames and sizes in a ZIP without extracting paths."""
    return await _call(
        lambda: local(lambda: current_service().artifacts.archive(artifact_id))
    )


@tool(read_only=False)
async def archive_member(artifact_id: str, name: str):
    """Copy one exact ZIP member into its own artifact for reading, PDF extraction or submission."""
    return await _call(
        lambda: local(lambda: current_service().artifacts.member(artifact_id, name))
    )


@tool(read_only=True)
async def read_pdf(artifact_id: str, page: int = 0):
    """Extract the existing text layer from a zero-based PDF page; does not run OCR.
    Use render_pdf_page for diagrams even when text exists, or when needs_ocr is true."""
    return await _call(
        lambda: local(lambda: current_service().artifacts.pdf_text(artifact_id, page))
    )


@tool(read_only=True)
async def render_pdf_page(artifact_id: str, page: int = 0, dpi: int = 120):
    """Return a PDF page as a native MCP image for visual reading. Zero-based pages.
    Use for diagrams or when read_pdf returns needs_ocr. Requires an image-capable client/model."""

    async def run():
        data, metadata = await local(
            lambda: current_service().artifacts.pdf_image(artifact_id, page, dpi)
        )
        response = result({"ok": True, "data": metadata})
        response.content.append(
            ImageContent(
                type="image", mimeType="image/png", data=base64.b64encode(data).decode()
            )
        )
        return response

    return await _call(run)


@tool(read_only=False)
async def upload_artifact(data_base64: str, filename: str = "solution.out"):
    """Store a base64-encoded file up to CCC_MAX_FILE_BYTES; return its artifact_id.
    For large local files use python -m ccc_mcp --url <MCP_URL> upload <path> instead of generating base64 in chat."""

    def save():
        limit = current_service().client.settings.max_bytes
        if len(data_base64) > 4 * ((limit + 2) // 3):
            raise ValueError("File exceeds CCC_MAX_FILE_BYTES")
        return current_service().artifacts.save(
            base64.b64decode(data_base64, validate=True), filename
        )

    return await _call(lambda: local(save))


@tool(read_only=False)
async def import_solution(relative_path: str):
    """Import a shared-volume file from this account's inbox. Paths are relative to inbox.
    Returns artifact_id for submit_solution. Supports files up to CCC_MAX_FILE_BYTES."""
    return await _call(
        lambda: local(lambda: current_service().artifacts.import_file(relative_path))
    )


@tool(read_only=False)
async def submit_solution(
    contest: str,
    level: int,
    file_id: str,
    solution: str | None = None,
    artifact_id: str | None = None,
    filename: str = "solution.out",
    include_case_details: bool = False,
):
    """Submit exactly one text solution OR artifact. Use artifact_id for large outputs.
    Returns evaluation, score and cooldownSec.
    Failed-case previews are bounded and use zero-based case_index; full_result preserves the complete report.
    No automatic retries or file-ID guessing. Check evaluation.isCorrect, not only ok."""

    async def run():
        if (solution is None) == (artifact_id is None):
            raise ValueError("Supply exactly one of solution or artifact_id")
        payload = (
            solution.encode("utf-8")
            if solution is not None
            else await local(
                lambda: current_service().artifacts.path(artifact_id).read_bytes()
            )
        )
        feedback = await current_service().submit(
            contest, level, file_id, payload, filename
        )
        evaluation = feedback.get("evaluation") if isinstance(feedback, dict) else None
        cases = evaluation.get("cases") if isinstance(evaluation, dict) else None
        if (
            isinstance(cases, list)
            and cases
            and all(isinstance(case, dict) for case in cases)
            and not include_case_details
        ):
            try:
                full = await local(
                    lambda: current_service().artifacts.save(
                        json.dumps(feedback).encode(), "submission-result.json"
                    )
                )
            except (OSError, ValueError):
                feedback["storage_warning"] = (
                    "Could not save the report; complete upstream feedback is returned inline."
                )
                return feedback
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
            feedback["full_result"] = full
        return feedback

    return await _call(run)


@tool(read_only=True)
async def participant_state(contest: str):
    """Read game-engine participant state."""

    async def run():
        return (
            await current_service().request(
                contest, "GET", "/api/game-engine/participant/state"
            )
        ).json()

    return await _call(run)


@tool(read_only=True)
async def game_leaderboard(
    contest: str, scope: Literal["WORLD", "COUNTRY", "LOCATION"] = "WORLD"
):
    """Read leaderboard using the actual website scopes."""

    async def run():
        return (
            await current_service().request(
                contest, "GET", "/api/game-engine/leaderboard", params={"scope": scope}
            )
        ).json()

    return await _call(run)


@tool(read_only=False)
async def download_certificate(contest_id: int, certificate_type: str):
    """Download a certificate listed by my_certificates as a PDF artifact."""

    async def run():
        response = await current_service().client.request(
            "GET", f"/api/certificates/{contest_id}/{segment(certificate_type)}"
        )
        return await local(
            lambda: current_service().artifacts.save(
                response.content, f"{contest_id}-{certificate_type}.pdf"
            )
        )

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
            raise ValueError(
                "Use dedicated account tools; raw authentication endpoints are disabled"
            )
        response = await current_service().client.request(
            method, path, params=_params(query), json_body=body
        )
        if response_format == "file":
            return current_service().artifacts.save(response.content, filename)
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
            contest, method, path, params=_params(query), json_body=body
        )
        if response_format == "file":
            return current_service().artifacts.save(response.content, filename)
        return response.json() if response.content else None

    return await _call(run)
