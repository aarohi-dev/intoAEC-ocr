import json
from unittest.mock import AsyncMock, patch

import fitz
import httpx
import pytest

from boqparser.adapter.ocr_adapter import PageOCRResult
from boqparser.extraction_service import ExtractionError, OllamaBOQExtractor, OllamaSettings, chunk_page_text


def test_chunk_page_text_preserves_lines_and_bounds():
    chunks = chunk_page_text("row one\nrow two\nrow three\n", 15)

    assert "".join(chunks) == "row one\nrow two\nrow three\n"
    assert all(len(chunk) <= 15 for chunk in chunks)


def test_chunk_page_text_hard_splits_only_oversized_lines():
    chunks = chunk_page_text("short\n" + "x" * 17 + "\n", 10)

    assert "short\n" in chunks
    assert "".join(chunks) == "short\n" + "x" * 17 + "\n"
    assert all(len(chunk) <= 10 for chunk in chunks)


@pytest.fixture
def boq_pdf(tmp_path):
    path = tmp_path / "boq.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Item Description Qty Unit Rate Amount\n1.1 Concrete 10 Cft 5 50")
    document.save(path)
    document.close()
    return str(path)


@pytest.mark.asyncio
async def test_ollama_primary_returns_existing_page_result_contract(boq_pdf):
    generate_requests = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})
        generate_requests.append(request)
        return httpx.Response(200, json={"response": '{"text":"1.1 Concrete 10 Cft 5 50", "is_table":true, "rows":[{"item_no":"1.1","description":"Concrete","quantity":10,"unit":"Cft","rate":5,"amount":50}]}'})

    async with httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler)) as client:
        extractor = OllamaBOQExtractor(OllamaSettings(base_url="http://ollama"), client)
        result = await extractor.extract_all(boq_pdf, 1)

    assert len(result) == 1
    assert isinstance(result[0], PageOCRResult)
    assert result[0].ocr_mode == "ollama"
    assert "<table>" in result[0].raw_html
    request_data = json.loads(generate_requests[0].content)
    assert request_data["format"]["properties"]["rows"]["items"]["type"] == "object"
    assert request_data["options"]["temperature"] == 0


@pytest.mark.asyncio
async def test_invalid_primary_response_is_rejected(boq_pdf):
    async with httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": "llama3.2"}]}))) as client:
        extractor = OllamaBOQExtractor(OllamaSettings(base_url="http://ollama"), client)
        extractor._generate = AsyncMock(return_value="not json")
        with pytest.raises((ExtractionError, ValueError)):
            await extractor.extract_all(boq_pdf, 1)


@pytest.mark.asyncio
async def test_primary_failure_calls_olmocr_only_as_fallback():
    primary = AsyncMock()
    primary.extract_all.side_effect = RuntimeError("ollama unavailable")
    fallback_result = [PageOCRResult(1, [], False, False, "fallback", None, None, "olmocr")]
    fallback = AsyncMock()
    fallback.extract_all.return_value = fallback_result

    with patch("boqparser.extraction_service.OllamaBOQExtractor", return_value=primary), patch("boqparser.extraction_service.OlmOCRFallbackExtractor", return_value=fallback):
        from boqparser.adapter.ocr_adapter import OCRAdapter

        result = await OCRAdapter(enable_olmocr_fallback=True).extract_all_async("document.pdf", 1)

    assert result == fallback_result
    primary.extract_all.assert_awaited_once()
    fallback.extract_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_primary_failure_does_not_call_olmocr_when_disabled():
    primary = AsyncMock()
    primary.extract_all.side_effect = RuntimeError("ollama unavailable")
    fallback = AsyncMock()

    with patch("boqparser.extraction_service.OllamaBOQExtractor", return_value=primary), patch("boqparser.extraction_service.OlmOCRFallbackExtractor", return_value=fallback):
        from boqparser.adapter.ocr_adapter import OCRAdapter

        with pytest.raises(Exception, match="Primary BOQ extraction failed"):
            await OCRAdapter(fallback_mode=False).extract_all_async("document.pdf", 1)

    primary.extract_all.assert_awaited_once()
    fallback.extract_all.assert_not_awaited()
