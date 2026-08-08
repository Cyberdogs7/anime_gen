"""LM Studio client (OpenAI-compatible). See DESIGN.md §11."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx


class LMStudioError(RuntimeError):
    pass


class LMStudioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, json_body: dict) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}{path}", json=json_body)
            resp.raise_for_status()
            return resp.json()

    def chat(self, messages: list[dict], model: str | None = None, temperature: float = 0.7,
             max_tokens: int = 4096, json_mode: bool = False, retries: int = 3) -> str:
        """Send a chat completion; returns the message content string.

        Note: LM Studio's response_format support varies by build (json_object /
        json_schema / text). JSON structure is enforced by prompt instruction +
        robust extraction in chat_json(), so json_mode is accepted for API
        compatibility but does not send response_format.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last: Exception | None = None
        for attempt in range(retries):
            try:
                data = self._post("/chat/completions", body)
                return data["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise LMStudioError(f"LM Studio chat failed after {retries} attempts: {last}")

    def chat_json(self, messages: list[dict], model: str | None = None,
                  temperature: float = 0.4, max_tokens: int = 4096, retries: int = 3) -> dict:
        """Chat completion with a JSON-object response, parsed.

        Retries (fresh generations) when the model truncates or emits unparseable
        JSON - small local models do this on long outputs.
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                content = self.chat(messages, model=model, temperature=temperature,
                                    max_tokens=max_tokens, json_mode=True, retries=1)
                return _extract_json(content)
            except (LMStudioError, Exception) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        raise LMStudioError(f"chat_json failed after {retries} attempts: {last}")

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base_url}/models")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction (direct parse, code fences, first balanced object)."""
    import re

    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise LMStudioError(f"No valid JSON in model output: {text[:200]!r}")
