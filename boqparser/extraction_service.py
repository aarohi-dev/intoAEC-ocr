"""BOQ extraction: PyMuPDF plus Ollama primary, olmOCR fallback."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional

import fitz
import httpx

from boqparser.adapter.ocr_adapter import OCRToken, PageOCRResult
from olmocr.prompts import PageResponse, build_no_anchoring_v4_yaml_prompt
from olmocr.train.front_matter import FrontMatterParser

logger = logging.getLogger(__name__)

BOQ_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["is_table", "rows"],
    "properties": {
        "text": {"type": "string"},
        "is_table": {"type": "boolean"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["item_no", "description", "quantity", "unit", "rate", "amount"],
                "properties": {
                    "item_no": {"type": "string"},
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": "string"},
                    "rate": {"type": ["number", "null"]},
                    "amount": {"type": ["number", "null"]},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class ExtractionError(RuntimeError):
    """Raised when an extraction path cannot return a valid result."""


@dataclass(frozen=True)
class OllamaSettings:
    base_url: str = "http://localhost:11434"
    model: str = "llama3.2"
    timeout: float = 360.0
    # 1500 chars ≈ 20-30 BOQ rows → JSON output stays well under 4096 tokens.
    chunk_size: int = 1500
    max_retries: int = 2
    # Enough for a 1500-char chunk's JSON output. Raise via OLLAMA_MAX_OUTPUT_TOKENS
    # env var if you use a larger chunk_size.
    max_output_tokens: int = 4096
    page_concurrency: int = 1

    @classmethod
    def from_env(cls) -> "OllamaSettings":
        import os

        return cls(
            base_url=os.environ.get("OLLAMA_BASE_URL", cls.base_url),
            model=os.environ.get("OLLAMA_MODEL", cls.model),
            timeout=float(os.environ.get("OLLAMA_TIMEOUT", cls.timeout)),
            chunk_size=int(os.environ.get("OLLAMA_CHUNK_SIZE", cls.chunk_size)),
            max_retries=int(os.environ.get("OLLAMA_MAX_RETRIES", cls.max_retries)),
            max_output_tokens=int(os.environ.get("OLLAMA_MAX_OUTPUT_TOKENS", cls.max_output_tokens)),
            page_concurrency=int(os.environ.get("OLLAMA_PAGE_CONCURRENCY", cls.page_concurrency)),
        )


def _strip_json_fences(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else value


def _salvage_truncated_json(text: str, page_num: int) -> Optional[dict[str, Any]]:
    """
    Attempt to recover usable BOQ rows from a JSON response that was cut off
    before the closing brace.  Walks the character stream tracking brace depth
    so it can pinpoint the last fully-closed row object, then reconstructs a
    syntactically valid JSON document from whatever arrived cleanly.

    Returns a valid payload dict on success, or None if nothing can be saved.
    """
    rows_start = text.find('"rows"')
    if rows_start == -1:
        return None
    array_open = text.find('[', rows_start)
    if array_open == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    last_complete_row_end = -1

    for i, c in enumerate(text[array_open:], start=array_open):
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                last_complete_row_end = i

    if last_complete_row_end == -1:
        return None

    # Extract the text field from the partial response (may be absent).
    text_val = ""
    text_m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if text_m:
        text_val = text_m.group(1)

    rows_fragment = text[array_open : last_complete_row_end + 1] + "]"
    candidate = f'{{"text":"{text_val}","is_table":true,"rows":{rows_fragment}}}'
    try:
        result = json.loads(candidate)
        n_rows = len(result.get("rows", []))
        logger.warning(
            f"Page {page_num}: response was truncated — salvaged {n_rows} complete row(s). "
            "If this happens often, reduce OLLAMA_CHUNK_SIZE further."
        )
        return result
    except json.JSONDecodeError:
        return None


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise ExtractionError(f"Invalid numeric BOQ value: {value!r}") from exc


def chunk_page_text(text: str, chunk_size: int) -> list[str]:
    """Split one page at line boundaries, hard-splitting only oversized lines."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        if len(line) > chunk_size:
            if current:
                chunks.append("".join(current))
                current, current_size = [], 0
            chunks.extend(line[start : start + chunk_size] for start in range(0, len(line), chunk_size))
            continue
        if current and current_size + len(line) > chunk_size:
            chunks.append("".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += len(line)
    if current:
        chunks.append("".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _row_to_html(row: dict[str, Any]) -> str:
    cells = [row.get("item_no"), row.get("description"), row.get("quantity"), row.get("unit"), row.get("rate"), row.get("amount")]
    return "<tr>" + "".join(f"<td>{'' if value is None else value}</td>" for value in cells) + "</tr>"


def _validate_page_payload(payload: Any, page_num: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ExtractionError(f"Llama response for page {page_num} is missing rows")
    normalized_rows = []
    for index, raw_row in enumerate(payload["rows"]):
        if not isinstance(raw_row, dict):
            raise ExtractionError(f"Llama row {index} on page {page_num} is not an object")
        description = raw_row.get("description")
        if description is None:
            raise ExtractionError(f"Llama row {index} on page {page_num} is missing description")
        normalized_rows.append(
            {
                "item_no": "" if raw_row.get("item_no") is None else str(raw_row.get("item_no")),
                "description": str(description).strip(),
                "quantity": _number(raw_row.get("quantity")),
                "unit": "" if raw_row.get("unit") is None else str(raw_row.get("unit")),
                "rate": _number(raw_row.get("rate")),
                "amount": _number(raw_row.get("amount")),
            }
        )
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise ExtractionError(f"Llama text for page {page_num} is not a string")
    return {"rows": normalized_rows, "text": text, "is_table": bool(payload.get("is_table", bool(normalized_rows)))}


def _payload_to_page_result(payload: dict[str, Any], page_num: int) -> PageOCRResult:
    rows = payload["rows"]
    header = "<tr><th>Item</th><th>Description</th><th>Quantity</th><th>Unit</th><th>Rate</th><th>Amount</th></tr>"
    raw_html = f"<table>{header}{''.join(_row_to_html(row) for row in rows)}</table>" if rows else None
    natural_text = payload["text"].strip() or None
    tokens = [OCRToken(text=natural_text or "", confidence=0.95, bbox=[0.0, 0.0, 0.0, 0.0], page_num=page_num, is_table=bool(raw_html), raw_html=raw_html)] if natural_text else []
    return PageOCRResult(page_num, tokens, bool(raw_html), False, natural_text or "", raw_html, None, "ollama")


class OllamaBOQExtractor:
    """Primary BOQ extractor using one reusable Ollama HTTP client."""

    def __init__(self, settings: OllamaSettings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or OllamaSettings.from_env()
        self.client = client
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None

    async def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                base_url=self.settings.base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.timeout, connect=10.0, write=30.0, pool=10.0),
            )
        return self.client

    async def verify(self) -> None:
        client = await self._http()
        try:
            response = await client.get("/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
            names = {model.get("name") for model in models}
            if self.settings.model not in names and f"{self.settings.model}:latest" not in names:
                raise ExtractionError(f"Ollama model '{self.settings.model}' is missing; run: ollama pull {self.settings.model}")
        except ExtractionError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ExtractionError(f"Ollama unavailable at {self.settings.base_url}: {exc}") from exc

    def _page_text(self, pdf_path: str, page_num: int) -> str:
        document = fitz.open(pdf_path)
        try:
            if page_num < 1 or page_num > document.page_count:
                raise ExtractionError(f"Page {page_num} outside PDF range")
            return document.load_page(page_num - 1).get_text("text") or ""
        finally:
            document.close()

    async def _generate(self, prompt: str) -> str:
        client = await self._http()
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await client.post(
                    "/api/generate",
                    json={
                        "model": self.settings.model,
                        "prompt": prompt,
                        "stream": False,
                        "format": BOQ_RESPONSE_SCHEMA,
                        "keep_alive": "10m",
                        # num_predict=-1 → Ollama stops at the natural EOS token.
                        # With forced JSON schema mode the model knows exactly when
                        # the object is closed, so it stops itself without needing
                        # an artificial token cap.  A hard cap (e.g. 4096) cuts off
                        # dense pages mid-JSON and causes parse failures.
                        "options": {"temperature": 0, "num_predict": -1},
                    },
                )
                response.raise_for_status()
                try:
                    text = response.json()["response"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise ExtractionError("Malformed Ollama response") from exc
                if not isinstance(text, str) or not text.strip():
                    raise ExtractionError("Ollama returned an empty response")
                # Warn if the response looks truncated, but let the caller decide
                # whether to salvage or fail.  We no longer raise here because
                # _salvage_truncated_json can often recover usable rows.
                if not text.strip().endswith("}"):
                    logger.warning(
                        "Ollama response does not end with '}' — may be truncated. "
                        "Attempting JSON salvage before deciding to fail."
                    )
                return text
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt < self.settings.max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
            except httpx.ReadTimeout as exc:
                raise ExtractionError(
                    f"Ollama generation exceeded OLLAMA_TIMEOUT={self.settings.timeout:g}s; "
                    "increase OLLAMA_TIMEOUT or reduce OLLAMA_CHUNK_SIZE"
                ) from exc
            except ExtractionError:
                raise
        error_detail = f"{type(last_error).__name__}: {last_error!r}" if last_error else "unknown error"
        raise ExtractionError(f"Ollama inference failed after retries: {error_detail}") from last_error

    async def extract_page(self, pdf_path: str, page_num: int) -> PageOCRResult:
        page_text = self._page_text(pdf_path, page_num)
        if not page_text.strip():
            raise ExtractionError(f"PyMuPDF extracted no usable text from page {page_num}")
        chunks = chunk_page_text(page_text, self.settings.chunk_size)
        outputs = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            prompt = (
                "Convert this BOQ page chunk into JSON only. Do not use Markdown or explanation. "
                "Preserve item order and source values; never invent missing values. Use null for unknown numeric values. "
                "This is chunk " + str(chunk_index) + " of " + str(len(chunks)) + " for page " + str(page_num) + ". "
                'Schema: {"is_table":true, "rows":[{"item_no":"string","description":"string","quantity":number|null,"unit":"string","rate":number|null,"amount":number|null}]}\n'
                f"Source text:\n{chunk}"
            )
            raw = _strip_json_fences(await self._generate(prompt))
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Primary parse failed — attempt to salvage complete rows
                payload = _salvage_truncated_json(raw, page_num)
                if payload is None:
                    raise ExtractionError(
                        f"Page {page_num} chunk {chunk_index}: Ollama returned unparseable JSON "
                        "and salvage found no complete rows. "
                        "Try reducing OLLAMA_CHUNK_SIZE in .env."
                    )
            outputs.append(_validate_page_payload(payload, page_num))
        rows = [row for output in outputs for row in output["rows"]]
        text = "\n".join(output["text"] for output in outputs if output.get("text")) or page_text.strip()
        if not rows:
            raise ExtractionError(f"Llama returned no structured BOQ rows for page {page_num}")
        return _payload_to_page_result({"rows": rows, "text": text, "is_table": bool(rows)}, page_num)

    async def extract_all(self, pdf_path: str, total_pages: int) -> list[PageOCRResult]:
        await self.verify()
        semaphore = asyncio.Semaphore(max(1, self.settings.page_concurrency))

        async def process(page_num: int) -> PageOCRResult:
            async with semaphore:
                return await self.extract_page(pdf_path, page_num)

        return await asyncio.gather(*(process(page_num) for page_num in range(1, total_pages + 1)))


class OlmOCRFallbackExtractor:
    """Existing olmOCR endpoint used only after the primary path fails."""

    def __init__(self, server_url: str, model_name: str = "olmocr", timeout: float = 120.0, client: httpx.AsyncClient | None = None):
        self.server_url = server_url.rstrip("/")
        self.model_name = model_name
        self.timeout = timeout
        self.client = client
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None

    async def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.timeout)
        return self.client

    async def extract_all(self, pdf_path: str, total_pages: int) -> list[PageOCRResult]:
        results = []
        document = fitz.open(pdf_path)
        try:
            for page_num in range(1, total_pages + 1):
                page = document.load_page(page_num - 1)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                payload = {"model": self.model_name, "messages": [{"role": "user", "content": [{"type": "text", "text": build_no_anchoring_v4_yaml_prompt()}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}]}], "max_tokens": 8000, "temperature": 0.1}
                response = await (await self._http()).post(f"{self.server_url}/chat/completions", json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                front_matter, text = FrontMatterParser()._extract_front_matter_and_text(content)
                page_response = FrontMatterParser(front_matter_class=PageResponse)._parse_front_matter(front_matter, text)
                text = page_response.natural_text or ""
                tokens = [OCRToken(line, 0.95, [0.0, 0.0, 0.0, 0.0], page_num, page_response.is_table, page_response.is_diagram) for line in text.splitlines() if line.strip()]
                results.append(PageOCRResult(page_num, tokens, page_response.is_table, page_response.is_diagram, text, None, page_response.primary_language, "olmocr"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ExtractionError(f"olmOCR fallback failed: {exc}") from exc
        finally:
            document.close()
        return results