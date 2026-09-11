"""Offload uploaded documents to Docling Serve; retain content-core elsewhere."""

import asyncio
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from content_core import ContentCoreConfig
from content_core import check_file_support as core_check_file_support
from content_core import extract_content as core_extract_content
from content_core.common import ExtractionOutput
from content_core.common.state import FileSupport
from content_core.config import get_default_config
from content_core.processors.document.docling import DOCLING_SUPPORTED
from loguru import logger

from open_notebook.exceptions import ConfigurationError, ExternalServiceError


def _endpoint() -> str:
    value = os.getenv("DOCLING_SERVE_URL", "").strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(
            "DOCLING_SERVE_URL must be an HTTP(S) base URL without credentials, query or fragment."
        )
    return value


def remote_docling_configured() -> bool:
    """Configuration availability, like remote Crawl4AI; not a live health probe."""
    try:
        return bool(_endpoint())
    except ConfigurationError:
        return False


async def check_file_support(
    file_path: str,
    config: ContentCoreConfig | None = None,
) -> FileSupport:
    """Share content-core's content-based identification with remote dispatch."""
    cfg = config or get_default_config()
    support = await core_check_file_support(file_path, config=cfg)
    if (
        remote_docling_configured()
        and cfg.document_engine in ("auto", "docling")
        and support.identified_type in DOCLING_SUPPORTED
    ):
        return support.model_copy(
            update={"supported": True, "processor": "docling-serve", "reason": None}
        )
    return support


def _json(response: httpx.Response) -> dict:
    # Do not include upstream bodies, URLs or credentials in job errors/logs.
    if not response.is_success:
        raise ExternalServiceError(
            f"Docling Serve returned HTTP {response.status_code}."
        )
    try:
        result = response.json()
    except ValueError:
        raise ExternalServiceError("Docling Serve returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise ExternalServiceError("Docling Serve returned an invalid response.")
    return result


async def _convert(
    file_path: str, mime: str, cfg: ContentCoreConfig, endpoint: str
) -> ExtractionOutput:
    try:
        timeout = float(os.getenv("DOCLING_SERVE_TIMEOUT", "600"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except ValueError:
        raise ConfigurationError(
            "DOCLING_SERVE_TIMEOUT must be a positive number of seconds."
        ) from None

    path = Path(file_path)
    key = os.getenv("DOCLING_SERVE_API_KEY", "")
    headers = {"X-Api-Key": key} if key else {}
    fields = {
        "to_formats": "md",
        "target_type": "inbody",
        "image_export_mode": "placeholder",
        "do_ocr": str(cfg.docling_ocr).lower(),
        "do_formula_enrichment": str(cfg.docling_formulas).lower(),
        "do_picture_description": str(cfg.docling_vision).lower(),
        "do_chart_extraction": str(cfg.docling_vision).lower(),
        "abort_on_error": "true",
    }
    logger.info("Extracting uploaded document via Docling Serve")
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                headers=headers,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                # Send bytes, not an Open Notebook path or a URL for Docling to fetch.
                with path.open("rb") as document:
                    task = _json(
                        await client.post(
                            f"{endpoint}/v1/convert/file/async",
                            data=fields,
                            files={"files": (path.name, document, mime)},
                        )
                    )
                task_id = task.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,128}", task_id
                ):
                    raise ExternalServiceError(
                        "Docling Serve returned an invalid task ID."
                    )
                while True:
                    status = task.get("task_status")
                    if status == "success":
                        break
                    if status not in ("pending", "started"):
                        raise ExternalServiceError(
                            "Docling Serve conversion failed or returned an unknown task status."
                        )
                    task = _json(
                        await client.get(
                            f"{endpoint}/v1/status/poll/{task_id}",
                            params={"wait": 5},
                        )
                    )
                    if task.get("task_status") in ("pending", "started"):
                        await asyncio.sleep(1)
                result = _json(await client.get(f"{endpoint}/v1/result/{task_id}"))
    except (TimeoutError, httpx.TimeoutException):
        raise ExternalServiceError(
            "Docling Serve conversion timed out; the server task may still be running."
        ) from None
    except httpx.RequestError:
        raise ExternalServiceError("Could not connect to Docling Serve.") from None

    document_result = result.get("document")
    if (
        result.get("status") != "success"
        or result.get("errors")
        or not isinstance(document_result, dict)
    ):
        raise ExternalServiceError(
            "Docling Serve did not return a complete conversion."
        )
    markdown = document_result.get("md_content")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ExternalServiceError("Docling Serve returned no text content.")
    return ExtractionOutput(
        content=markdown,
        title=path.stem,
        source_type="file",
        identified_type=mime,
        metadata={"extraction_engine": "docling-serve", "docling_format": "markdown"},
    )


async def extract_content(
    *,
    url: str | None = None,
    file_path: str | None = None,
    content: str | None = None,
    config: ContentCoreConfig | None = None,
) -> ExtractionOutput:
    """Remote extraction for supported uploads; URLs/audio/text retain their route."""
    cfg = config or get_default_config()
    if (
        file_path
        and not url
        and not content
        and cfg.document_engine in ("auto", "docling")
    ):
        endpoint = _endpoint()
        if endpoint:
            support = await check_file_support(file_path, config=cfg)
            if support.processor == "docling-serve":
                return await _convert(file_path, support.identified_type, cfg, endpoint)
    return await core_extract_content(
        url=url, file_path=file_path, content=content, config=config
    )
