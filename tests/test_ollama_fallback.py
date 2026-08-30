import httpx
import pytest
import fitz
from unittest.mock import AsyncMock, patch

from olmocr.ollama_fallback import OllamaClient, OllamaConfig, OllamaFallbackError, chunk_text, extract_page_with_ollama, iter_page_text
from olmocr.pipeline import PageResult, process_page
from olmocr.prompts import PageResponse


class FallbackArgs:
    max_page_retries = 1
    enable_ollama_fallback = True
    target_longest_image_dim = 1288
    guided_decoding = False
    server = "http://unavailable/v1"
    model = "olmocr"


@pytest.mark.asyncio
async def test_exhausted_primary_retries_activate_fallback():
    fallback_result = PageResult("doc.pdf", 1, PageResponse(None, True, 0, False, False, "fallback"), 0, 0, True, True)
    with patch("olmocr.pipeline.try_single_page_with_backoff", new=AsyncMock(return_value=None)), patch(
        "olmocr.pipeline.make_configured_fallback_result", new=AsyncMock(return_value=fallback_result)
    ) as fallback:
        result = await process_page(FallbackArgs(), 0, "doc.pdf", "doc.pdf", 1)

    assert result is fallback_result
    fallback.assert_awaited_once()


def test_chunk_text_is_bounded_and_does_not_drop_content():
    text = "abcdefghij"
    chunks = chunk_text(text, 3)

    assert chunks == ["abc", "def", "ghi", "j"]
    assert "".join(chunks) == text
    assert chunk_text("   ", 3) == []


@pytest.mark.asyncio
async def test_ollama_generate_returns_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"response": "converted text"}))
    async with httpx.AsyncClient(base_url="http://ollama", transport=transport) as http_client:
        client = OllamaClient(OllamaConfig(max_retries=0), http_client=http_client)
        result = await client.generate("page text")

    assert result == "converted text"


@pytest.mark.asyncio
async def test_ollama_verify_reports_missing_model():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": "other:latest"}]}))
    async with httpx.AsyncClient(base_url="http://ollama", transport=transport) as http_client:
        client = OllamaClient(OllamaConfig(max_retries=0), http_client=http_client)
        with pytest.raises(OllamaFallbackError, match="missing"):
            await client.verify()


@pytest.mark.asyncio
async def test_ollama_generate_rejects_empty_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"response": ""}))
    async with httpx.AsyncClient(base_url="http://ollama", transport=transport) as http_client:
        client = OllamaClient(OllamaConfig(max_retries=0), http_client=http_client)
        with pytest.raises(OllamaFallbackError, match="empty"):
            await client.generate("page text")


@pytest.mark.asyncio
async def test_pdf_pages_are_extracted_individually_and_chunked(tmp_path):
    pdf_path = tmp_path / "multi-page.pdf"
    document = fitz.open()
    for page_number in range(101):
        page = document.new_page()
        page.insert_text((72, 72), f"page {page_number + 1}")
    document.save(pdf_path)
    document.close()

    assert next(iter_page_text(str(pdf_path), 1)).strip() == "page 1"
    assert next(iter_page_text(str(pdf_path), 101)).strip() == "page 101"

    client = OllamaClient(OllamaConfig(chunk_size=4))
    client.generate = AsyncMock(side_effect=lambda prompt: prompt.split("Extracted page text:\n", 1)[-1])
    response, input_chars, _ = await extract_page_with_ollama(str(pdf_path), 1, client)

    assert response.natural_text is not None
    assert "page 1" in response.natural_text
    assert input_chars == len("page 1\n")


@pytest.mark.asyncio
async def test_empty_pdf_page_returns_empty_page_response(tmp_path):
    pdf_path = tmp_path / "empty.pdf"
    document = fitz.open()
    document.new_page()
    document.save(pdf_path)
    document.close()

    client = OllamaClient()
    response, input_chars, output_chars = await extract_page_with_ollama(str(pdf_path), 1, client)

    assert response.natural_text is None
    assert input_chars == 0
    assert output_chars == 0