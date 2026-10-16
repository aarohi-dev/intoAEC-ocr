"""
Local LLM Adapter (Ollama) for Bounded Semantic Mapping.

Target Hardware Configuration:
- Max 8 GB system RAM / 4 GB GPU VRAM.
- Recommended small model: 'qwen2.5:1.5b' (fast, ~1.2 GB VRAM, excellent JSON output).
- Alternative: 'llama3.2:1b' (~1.3 GB VRAM).

Constraints:
- Only bounded text snippets (< 500 tokens) are sent to the model (e.g. unfamiliar header rows).
- Never send whole document pages or whole PDFs to the model.
- 100% offline resilient: if Ollama is not installed or unreachable, returns fallback
  deterministic results with review flags.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")


class OllamaAdapter:
    """Bounded local LLM adapter for ambiguous construction header and field interpretation."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        timeout: float = 8.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._available: Optional[bool] = None
        self._cache: Dict[str, Dict[str, str]] = {}

    def is_available(self) -> bool:
        """Check if Ollama server is running locally."""
        if self._available is not None:
            return self._available

        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                self._available = (resp.status == 200)
                if self._available:
                    logger.info(f"Ollama server detected at {self.base_url} (model: {self.model})")
        except Exception:
            self._available = False
            logger.debug("Ollama server not reachable. Running in deterministic heuristic mode.")

        return self._available

    def classify_headers(self, header_candidates: List[str]) -> Dict[str, str]:
        """
        Ask local model to map unfamiliar column labels to standard roles:
        [item_no, description, quantity, unit, rate, amount, unknown].
        """
        if not header_candidates or not self.is_available():
            return {}

        cache_key = "|".join(sorted(header_candidates))
        if cache_key in self._cache:
            return self._cache[cache_key]

        prompt = (
            "You are a construction estimation AI. Map these table column headers to one of: "
            "['item_no', 'description', 'quantity', 'unit', 'rate', 'amount', 'unknown'].\n\n"
            f"Headers: {json.dumps(header_candidates)}\n\n"
            "Respond ONLY with a JSON object mapping each input header string to its category."
        )

        try:
            payload = json.dumps({
                "model": self.model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 256,
                }
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                response_text = body.get("response", "{}")
                mapping = json.loads(response_text)
                clean_map = {str(k): str(v) for k, v in mapping.items() if isinstance(k, str) and isinstance(v, str)}
                self._cache[cache_key] = clean_map
                return clean_map
        except Exception as exc:
            logger.debug(f"Ollama header classification fallback triggered: {exc}")
            return {}
