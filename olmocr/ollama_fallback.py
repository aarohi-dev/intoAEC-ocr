"""PyMuPDF and Ollama fallback for pages the primary OCR path cannot process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from olmocr.prompts import PageResponse, build_no_anchoring_v4_yaml_prompt

logger = logging.getLogger(__name__)


class OllamaFallbackError(RuntimeError):
    """Raised when the Ollama fallback cannot produce a usable response."""


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = "http://localhost:11434"
    model: str = "llama3.2:latest"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    chunk_size: int = 12000


class OllamaClient:
    """Small connection-reusing client for the Ollama generate API."""

    def __init__(self, config: OllamaConfig | None = None, http_client: httpx.AsyncClient | None = None):
        self.config = config or OllamaConfig()
        self._http_client = http_client
        self._owns_http_client = http_client is None

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=httpx.Timeout(self.config.timeout_seconds),
            )
        return self._http_client

    async def verify(self) -> None:
        """Check Ollama reachability and that the configured model is installed."""
        client = await self._client()
        try:
            response = await client.get("/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
            available = {model.get("name") for model in models}
            if self.config.model not in available:
                raise OllamaFallbackError(
                    f"Ollama model '{self.config.model}' is missing. Install it with: ollama pull {self.config.model}"
                )
        except OllamaFallbackError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaFallbackError(f"Ollama is unavailable at {self.config.base_url}: {exc}") from exc

    async def generate(self, prompt: str) -> str:
        client = await self._client()
        payload = {"model": self.config.model, "prompt": prompt, "stream": False}
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await client.post("/api/generate", json=payload)
                response.raise_for_status()
                result = response.json()
                text = result.get("response")
                if not isinstance(text, str) or not text.strip():
                    raise OllamaFallbackError("Ollama returned an empty or malformed response")
                return text.strip()
            except OllamaFallbackError as exc:
                last_error = exc
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = exc
            if attempt < self.config.max_retries:
                await asyncio.sleep(0.5 * (2**attempt))
        raise OllamaFallbackError(f"Ollama generation failed after retries: {last_error}") from last_error


def iter_page_text(pdf_path: str, page_num: int) -> Iterator[str]:
    """Yield text for one page, opening only the requested page's document context."""
    try:
        import fitz
    except ImportError as exc:
        raise OllamaFallbackError("PyMuPDF is required for the Ollama fallback. Install it with: pip install PyMuPDF") from exc

    document = fitz.open(pdf_path)
    try:
        if page_num < 1 or page_num > document.page_count:
            raise OllamaFallbackError(f"Page {page_num} is outside PDF page range 1-{document.page_count}")
        yield document.load_page(page_num - 1).get_text("text") or ""
    finally:
        document.close()


def chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split text into bounded chunks without crossing explicit page content."""
    if not text.strip():
        return []
    return [text[start : start + chunk_size] for start in range(0, len(text), chunk_size)]


async def extract_page_with_ollama(
    pdf_path: str,
    page_num: int,
    client: OllamaClient,
) -> tuple[PageResponse, int, int]:
    """Extract and transform one PDF page while retaining the existing PageResponse schema."""
    page_text = next(iter_page_text(pdf_path, page_num))
    if not page_text.strip():
        return (
            PageResponse(None, True, 0, False, False, None),
            0,
            0,
        )

    chunks = chunk_text(page_text, client.config.chunk_size)
    prompt_prefix = (
        "Return only the natural text for this PDF page. Preserve tables as markdown, "
        "do not invent content, and return an empty response if no text is present.\n\n"
        f"Existing OCR instructions:\n{build_no_anchoring_v4_yaml_prompt()}\n\n"
    )
    outputs = []
    input_chars = 0
    output_chars = 0
    for chunk in chunks:
        input_chars += len(chunk)
        output = await client.generate(f"{prompt_prefix}Extracted page text:\n{chunk}")
        outputs.append(output)
        output_chars += len(output)

    natural_text = "\n".join(outputs).strip() or None
    return PageResponse(None, True, 0, False, False, natural_text), input_chars, output_chars