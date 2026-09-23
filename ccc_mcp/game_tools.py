"""Contest-scoped tools; importing this module registers them."""

import base64
import json
from typing import Any, Literal
from urllib.parse import unquote

from mcp.types import ImageContent

from .context import current_service
from .service import compact_progress, segment
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


@tool(read_only=True)
async def game_info(contest: str):
    """Get fresh progress, game metadata, inputFiles IDs and a resume reference.
    Accessible levels are hints derived from progress; CCC decides actual access."""
    return await _call(lambda: current_service().info(contest))


@tool(read_only=False)
async def prepare_level(contest: str, level: int):
    """Get fresh progress, exact inputFiles IDs, level ZIP and extracted file artifact IDs in one call.
    Then render PDF artifacts or read/download inputs. Does not start training or submit anything."""

    async def run():
        service = current_service()
        service.validate_level(level)
        info = await service.info(contest)
        archive = await service.asset(
            info["contest_slug"],
            f"/api/contestant/level/{level}/files",
            f"level-{level}.zip",
        )
        files = await local(lambda: service.artifacts.unpack(archive["artifact_id"]))
        return {
            "contest": info["contest_slug"],
            "level": level,
            "level_info": next(
                (entry for entry in info["levels"] if entry["level"] == level), None
            ),
            "participant": info["participant"],
            "archive": archive,
            "files": files,
            "transfer": {
                "upload_url": service.artifacts.transfer_url,
                "auth_header": "X-CCC-Session",
                "max_file_bytes": service.client.settings.max_bytes,
                "download": 'curl --fail --output input.zip --header "X-CCC-Session: $CCC_SESSION" DOWNLOAD_URL',
                "upload": 'curl --fail --header "X-CCC-Session: $CCC_SESSION" --header "Content-Type: application/octet-stream" --data-binary @answer.out UPLOAD_URL',
            },
        }

    return await _call(run)


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
    """Download one input to an artifact. Use download_url from a local shell for the complete file."""

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
    length: int = 8192,
    encoding: Literal["text", "base64"] = "text",
):
    """Preview a small byte range. For large inputs download download_url locally; do not loop over offsets in MCP."""
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
    """Optional text extraction for known text-only PDFs. For challenge statements use render_pdf_page first;
    an existing text layer may contain only footers and omit the actual instructions."""
    return await _call(
        lambda: local(lambda: current_service().artifacts.pdf_text(artifact_id, page))
    )


@tool(read_only=True)
async def render_pdf_page(
    artifact_id: str, page: int = 0, dpi: int = 100, pages: list[int] | None = None
):
    """Read challenge statements visually; no text extraction needed. Native images, zero-based pages.
    Use pages=[0,1,2,3] for up to 6 pages in one call, or page for one page. File metadata includes total_pages."""

    async def run():
        selected = pages if pages is not None else [page]
        if not 1 <= len(selected) <= 6:
            raise ValueError("Request 1..6 pages per call")
        total = await local(lambda: current_service().artifacts.pdf_pages(artifact_id))
        if any(p < 0 or p >= total for p in selected):
            raise ValueError(f"PDF has {total} pages; use page 0..{total - 1}")
        images, metadata, size = [], [], 0
        for p in selected:
            data, info = await local(
                lambda: current_service().artifacts.pdf_image(artifact_id, p, dpi)
            )
            size += len(data)
            if size > current_service().client.settings.max_bytes:
                raise ValueError(
                    "Images exceed CCC_MAX_FILE_BYTES; request fewer pages or lower dpi"
                )
            metadata.append(info)
            images.append(
                ImageContent(
                    type="image",
                    mimeType="image/png",
                    data=base64.b64encode(data).decode(),
                )
            )
        response = result(
            {
                "ok": True,
                "data": metadata[0]
                if pages is None
                else {"total_pages": total, "pages": metadata},
            }
        )
        response.content.extend(images)
        return response

    return await _call(run)


@tool(read_only=False)
async def upload_artifact(data_base64: str, filename: str = "solution.out"):
    """Store a small base64 file. For large outputs POST raw bytes to prepare_level.transfer.upload_url
    using curl --data-binary @answer.out and X-CCC-Session; then submit the returned artifact_id."""

    def save():
        limit = current_service().client.settings.max_bytes
        if len(data_base64) > 4 * ((limit + 2) // 3):
            raise ValueError("File exceeds CCC_MAX_FILE_BYTES")
        return current_service().artifacts.save(
            base64.b64decode(data_base64, validate=True), filename
        )

    return await _call(lambda: local(save))


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
        return feedback if include_case_details else compact_progress(feedback)

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
