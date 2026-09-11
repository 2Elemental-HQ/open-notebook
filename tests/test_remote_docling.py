"""The remote boundary must work without the local Docling runtime."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from content_core import ContentCoreConfig
from content_core.common import ExtractionOutput

from open_notebook.utils import remote_docling as remote
from open_notebook.utils.runtime_capabilities import docling_available


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://docling:5001")
    monkeypatch.setenv("DOCLING_SERVE_API_KEY", "test-key")
    monkeypatch.delenv("DOCLING_SERVE_TIMEOUT", raising=False)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n%test document")
    return str(path)


def mock_server(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        remote.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )


@pytest.mark.asyncio
async def test_upload_poll_result_auth_and_options(configured, pdf, monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        assert request.headers["X-Api-Key"] == "test-key"
        if request.method == "POST":
            body = await request.aread()
            assert b"%PDF-1.4" in body
            for name, value in [
                ("do_ocr", "false"),
                ("do_formula_enrichment", "true"),
                ("do_picture_description", "true"),
                ("do_chart_extraction", "true"),
            ]:
                assert f'name="{name}"\r\n\r\n{value}'.encode() in body
            return httpx.Response(
                200, json={"task_id": "job-1", "task_status": "pending"}
            )
        if "status/poll" in request.url.path:
            return httpx.Response(
                200, json={"task_id": "job-1", "task_status": "success"}
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "document": {"md_content": "# Converted"},
                "errors": [],
            },
        )

    mock_server(monkeypatch, handler)
    result = await remote.extract_content(
        file_path=pdf,
        config=ContentCoreConfig(
            document_engine="docling",
            docling_ocr=False,
            docling_formulas=True,
            docling_vision=True,
        ),
    )
    assert result.content == "# Converted"
    assert result.source_type == "file"
    assert result.title == "paper"
    assert result.metadata["extraction_engine"] == "docling-serve"
    assert calls == [
        "/v1/convert/file/async",
        "/v1/status/poll/job-1",
        "/v1/result/job-1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine,url,content",
    [
        ("simple", None, None),
        ("auto", "https://example.com", None),
        ("auto", None, "hello"),
    ],
)
async def test_other_routes_keep_content_core(
    configured, pdf, monkeypatch, engine, url, content
):
    fallback = AsyncMock(return_value=ExtractionOutput(content="original"))
    monkeypatch.setattr(remote, "core_extract_content", fallback)
    result = await remote.extract_content(
        file_path=pdf if not url and not content else None,
        url=url,
        content=content,
        config=ContentCoreConfig(document_engine=engine),
    )
    assert result.content == "original"
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_remote_config_keeps_local_route(pdf, monkeypatch):
    monkeypatch.delenv("DOCLING_SERVE_URL", raising=False)
    fallback = AsyncMock(return_value=ExtractionOutput(content="local"))
    monkeypatch.setattr(remote, "core_extract_content", fallback)
    assert (await remote.extract_content(file_path=pdf)).content == "local"


@pytest.mark.asyncio
async def test_image_preflight_without_local_docling(configured, tmp_path):
    from PIL import Image

    path = tmp_path / "scan.png"
    Image.new("RGB", (4, 4)).save(path)
    support = await remote.check_file_support(str(path))
    assert support.supported
    assert support.processor == "docling-serve"
    assert docling_available()
    from api.routers.sources import _assert_file_supported

    await _assert_file_supported(str(path))
    support = await remote.check_file_support(
        str(path), ContentCoreConfig(document_engine="simple")
    )
    assert not support.supported


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403, 302, 500])
async def test_http_failures_do_not_fallback_or_leak_body(
    configured, pdf, monkeypatch, code
):
    mock_server(
        monkeypatch,
        lambda req: httpx.Response(
            code, text="private-secret", headers={"Location": "http://other/"}
        ),
    )
    with pytest.raises(Exception, match="Docling") as exc:
        await remote.extract_content(file_path=pdf)
    assert "private-secret" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "partial_success", "document": {"md_content": "partial"}},
        {"status": "success", "document": {"md_content": ""}},
        {
            "status": "success",
            "document": {"md_content": "text"},
            "errors": ["private"],
        },
        [],
    ],
)
async def test_bad_results_fail(configured, pdf, monkeypatch, payload):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200, json={"task_id": "job-1", "task_status": "success"}
            )
        return httpx.Response(200, json=payload)

    mock_server(monkeypatch, handler)
    with pytest.raises(Exception, match="Docling"):
        await remote.extract_content(file_path=pdf)


@pytest.mark.asyncio
async def test_deadline_covers_pending_jobs(configured, pdf, monkeypatch):
    monkeypatch.setenv("DOCLING_SERVE_TIMEOUT", "0.02")

    async def handler(request):
        if request.method == "GET":
            await asyncio.sleep(1)
        return httpx.Response(200, json={"task_id": "job-1", "task_status": "pending"})

    mock_server(monkeypatch, handler)
    with pytest.raises(Exception, match="timed out"):
        await remote.extract_content(file_path=pdf)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/docling",
        "http://user:secret@docling",
        "http://docling?token=secret",
    ],
)
def test_invalid_endpoint_is_not_advertised(monkeypatch, url):
    monkeypatch.setenv("DOCLING_SERVE_URL", url)
    assert not remote.remote_docling_configured()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task",
    [
        {"task_id": "../../other", "task_status": "success"},
        {"task_id": "job-1", "task_status": "failure", "error_message": "private"},
    ],
)
async def test_bad_tasks_fail_without_followup(configured, pdf, monkeypatch, task):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=task)

    mock_server(monkeypatch, handler)
    with pytest.raises(Exception, match="Docling") as exc:
        await remote.extract_content(file_path=pdf)
    assert "private" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_source_graph_uses_remote_and_retains_original(
    configured, pdf, monkeypatch
):
    from types import SimpleNamespace

    from open_notebook.graphs import source

    monkeypatch.setattr(
        source.ContentSettings,
        "get_instance",
        AsyncMock(
            return_value=SimpleNamespace(
                default_content_processing_engine_doc="docling",
                default_content_processing_engine_url="crawl4ai",
                docling_ocr=False,
                docling_formulas=False,
                docling_vision=False,
            )
        ),
    )
    monkeypatch.setattr(
        source.ModelManager,
        "get_defaults",
        AsyncMock(return_value=SimpleNamespace(default_speech_to_text_model=None)),
    )
    calls = []

    async def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert b'name="do_ocr"\r\n\r\nfalse' in await request.aread()
            return httpx.Response(
                200, json={"task_id": "job-1", "task_status": "success"}
            )
        return httpx.Response(
            200, json={"status": "success", "document": {"md_content": "# Remote text"}}
        )

    mock_server(monkeypatch, handler)
    result = await source.content_process(
        {"content_state": {"file_path": pdf, "delete_source": False}}
    )
    assert result["extraction"].content == "# Remote text"
    from pathlib import Path

    assert Path(pdf).exists()
    assert len(calls) == 2
