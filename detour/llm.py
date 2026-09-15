"""Model access, provider-agnostic, stdlib only.

Gemini is the primary provider; the Anthropic path is kept because the
description rewriter was written against it first and either key works.

The key comes from a gitignored .env beside the project (see config.py) or
from a real environment variable, and is sent in a header. It is never put
in the URL: query strings end up in proxy logs, server access logs and
browser history, and a key that leaks that way is a key you have to rotate.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import ensure_loaded

# Before reading any default below: a GEMINI_MODEL set in .env must be
# visible at import time, not only once someone calls provider().
ensure_loaded()

GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Pinned rather than an alias, so a run is reproducible. Check `--models`
# for what a given key can actually reach: this list moves faster than any
# default written into source, and a stale default fails as a 404.
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
DEFAULT_ANTHROPIC_MODEL = os.environ.get("DETOUR_MODEL", "claude-haiku-4-5-20251001")


class LLMError(RuntimeError):
    pass


def provider() -> str | None:
    """Which provider this machine is configured for, if any."""
    ensure_loaded()
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _post(url: str, payload: dict, headers: dict, *, timeout: int = 90) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMError(f"request failed: {exc}") from exc


def list_gemini_models() -> list[str]:
    """Model ids this key can actually use.

    Worth having as a first-class helper rather than a hardcoded guess:
    model availability changes, and a 404 from a stale default is an
    unhelpful way to find that out.
    """
    ensure_loaded()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set")

    request = urllib.request.Request(
        f"{GEMINI_HOST}/models", headers={"x-goog-api-key": key}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LLMError(f"HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:300]}") from exc

    names = []
    for model in body.get("models", []):
        if "generateContent" in (model.get("supportedGenerationMethods") or []):
            names.append((model.get("name") or "").removeprefix("models/"))
    return sorted(n for n in names if n)


# --------------------------------------------------------------------------
# Tool-calling conversation
# --------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    """One model response: either tool calls to run, or final text."""

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.calls)

    @property
    def truncated(self) -> bool:
        """Output hit the token ceiling, so `text` is cut off mid-stream.

        Worth distinguishing from a malformed reply: truncated JSON is a
        budget problem to retry, malformed JSON is a model problem to report.
        """
        return self.finish_reason.upper() in ("MAX_TOKENS", "LENGTH")


class GeminiSession:
    """A multi-turn tool-calling conversation.

    Gemini's REST shape: the running history is a list of `contents`, each
    with a role of "user" or "model". A tool result is appended as a user
    turn carrying `functionResponse` parts, mirroring the `functionCall`
    parts the model produced.
    """

    def __init__(
        self,
        tools: list[dict],
        *,
        model: str | None = None,
        system: str = "",
        temperature: float = 0.0,
    ):
        ensure_loaded()
        self.key = os.environ.get("GEMINI_API_KEY")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set")

        self.model = model or DEFAULT_GEMINI_MODEL
        self.tools = tools
        self.system = system
        self.temperature = temperature
        self.contents: list[dict] = []
        self.calls_made = 0

    def _payload(self) -> dict:
        payload: dict[str, Any] = {
            "contents": self.contents,
            "generationConfig": {
                "temperature": self.temperature,
                # Reasoning models spend output tokens before the visible
                # reply. At 1400 a verdict could be cut off mid-JSON.
                "maxOutputTokens": 8192,
            },
        }
        if self.tools:
            payload["tools"] = [{"function_declarations": self.tools}]
        if self.system:
            payload["systemInstruction"] = {"parts": [{"text": self.system}]}
        return payload

    def _send(self) -> Turn:
        url = f"{GEMINI_HOST}/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.key}

        last: LLMError | None = None
        for attempt in range(3):
            try:
                body = _post(url, self._payload(), headers)
                break
            except LLMError as exc:
                last = exc
                if "HTTP 4" in str(exc) and "429" not in str(exc):
                    raise
                time.sleep(1.5 * (attempt + 1))
        else:
            raise last or LLMError("no response")

        candidates = body.get("candidates") or []
        if not candidates:
            blocked = body.get("promptFeedback", {}).get("blockReason")
            raise LLMError(f"no candidates{f' ({blocked})' if blocked else ''}")

        parts = candidates[0].get("content", {}).get("parts") or []
        self.contents.append({"role": "model", "parts": parts})

        turn = Turn(finish_reason=candidates[0].get("finishReason") or "")
        for part in parts:
            if "text" in part:
                turn.text += part["text"]
            call = part.get("functionCall")
            if call:
                turn.calls.append(ToolCall(call.get("name", ""), call.get("args") or {}))

        self.calls_made += len(turn.calls)
        return turn

    def ask(self, text: str) -> Turn:
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        return self._send()

    def give_results(self, results: list[tuple[str, Any]]) -> Turn:
        """Return tool outputs and let the model continue."""
        self.contents.append(
            {
                "role": "user",
                "parts": [
                    {"functionResponse": {"name": name, "response": {"result": value}}}
                    for name, value in results
                ],
            }
        )
        return self._send()


# --------------------------------------------------------------------------
# One-shot completion, used by the description rewriter
# --------------------------------------------------------------------------

def complete(prompt: str, *, max_tokens: int = 200) -> str | None:
    """Single-turn text completion on whichever provider is configured."""
    which = provider()

    if which == "gemini":
        try:
            body = _post(
                f"{GEMINI_HOST}/models/{DEFAULT_GEMINI_MODEL}:generateContent",
                {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens},
                },
                {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                timeout=30,
            )
        except LLMError:
            return None
        candidates = body.get("candidates") or []
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return text.strip() or None

    if which == "anthropic":
        try:
            body = _post(
                ANTHROPIC_URL,
                {
                    "model": DEFAULT_ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                {
                    "anthropic-version": "2023-06-01",
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                },
                timeout=30,
            )
        except LLMError:
            return None
        blocks = body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return text.strip() or None

    return None
