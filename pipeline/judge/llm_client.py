"""LLM client wrapper with streaming, caching, retry, and token tracking."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# Optional: route outbound traffic through a SOCKS / HTTP proxy. The original
# author's environment used a local SOCKS bridge; on a fresh install leave this
# unset (or export all_proxy / https_proxy explicitly before launching) — both
# httpx and the openai SDK honour these env variables.
# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route outbound traffic via local SOCKS bridge

from dotenv import load_dotenv
import httpx
import httpcore

# Load .env — search upward from this file
_here = Path(__file__).resolve().parent
for _up in [_here] + list(_here.parents):
    _env_path = _up / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        break

import openai


class LLMClient:
    """OpenAI-compatible client that always streams, caches, and retries."""

    BACKOFF_SCHEDULE = [30, 90, 180, 600, 1200]

    def __init__(self, cache_dir: str, token_log_path: str, api_key: str | None = None, base_url: str | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.token_log_path = Path(token_log_path)
        self.token_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._cumulative_tokens = {"prompt": 0, "completion": 0, "calls": 0}
        self.client = openai.OpenAI(
            api_key=api_key or os.environ["OPENAI_API_KEY"],
            base_url=base_url or os.environ["OPENAI_BASE_URL"],
        )

    def _content_hash(self, model: str, messages: list[dict], max_tokens: int, temperature: float) -> str:
        payload = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"

    def _load_cache(self, cache_key: str) -> str | None:
        path = self._cache_path(cache_key)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("response")
            except (json.JSONDecodeError, KeyError):
                return None
        return None

    def _save_cache(self, cache_key: str, model: str, response: str, est_tokens: int):
        path = self._cache_path(cache_key)
        data = {
            "model": model,
            "cache_key": cache_key,
            "response": response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "estimated_tokens": est_tokens,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _log_tokens(self, model: str, prompt_tokens: int, completion_tokens: int):
        self._cumulative_tokens["prompt"] += prompt_tokens
        self._cumulative_tokens["completion"] += completion_tokens
        self._cumulative_tokens["calls"] += 1
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cumulative": dict(self._cumulative_tokens),
        }
        with open(self.token_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _estimate_tokens(self, content) -> int:
        # Handle both string content and OpenAI-style list-of-parts content
        if isinstance(content, str):
            return max(1, len(content.split()) * 4 // 3)
        if isinstance(content, list):
            total = 0
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += max(1, len(part.get("text", "").split()) * 4 // 3)
                    elif part.get("type") in ("image_url", "image"):
                        # Rough estimate for one image ≈ 1500 tokens (Claude vision)
                        total += 1500
            return max(1, total)
        return 1

    def call(
        self,
        messages: list[dict],
        model: str = "claude-opus-4.6",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        cache_key: str | None = None,
    ) -> str:
        if cache_key is None:
            cache_key = self._content_hash(model, messages, max_tokens, temperature)

        cached = self._load_cache(cache_key)
        if cached is not None:
            return cached

        stream = self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )

        chunks = []
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        response = "".join(chunks)
        est_prompt = sum(self._estimate_tokens(m.get("content", "")) for m in messages)
        est_completion = self._estimate_tokens(response)
        self._save_cache(cache_key, model, response, est_prompt + est_completion)
        self._log_tokens(model, est_prompt, est_completion)
        return response

    def call_with_retry(
        self,
        messages: list[dict],
        model: str = "claude-opus-4.6",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        cache_key: str | None = None,
        max_retries: int = 5,
    ) -> str:
        last_error = None
        for attempt in range(max_retries):
            try:
                return self.call(messages, model, max_tokens, temperature, cache_key)
            except (openai.APIConnectionError, openai.RateLimitError, openai.APITimeoutError, openai.APIError, ConnectionError, TimeoutError, httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpcore.RemoteProtocolError, httpcore.ReadError) as e:
                # Note: we intentionally catch the broad openai.APIError base
                # class so that 400 "context too long" errors are retried —
                # our upstream pool routes randomly across keys with different
                # context-window limits, so retrying often succeeds.
                wait = self.BACKOFF_SCHEDULE[min(attempt, len(self.BACKOFF_SCHEDULE) - 1)]
                print(f"  [retry {attempt+1}/{max_retries}] {type(e).__name__}: {e}. Waiting {wait}s...")
                last_error = e
                time.sleep(wait)
            except openai.APIStatusError as e:
                if e.status_code in (429, 500, 502, 503, 504):
                    wait = self.BACKOFF_SCHEDULE[min(attempt, len(self.BACKOFF_SCHEDULE) - 1)]
                    print(f"  [retry {attempt+1}/{max_retries}] HTTP {e.status_code}. Waiting {wait}s...")
                    last_error = e
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"Failed after {max_retries} retries. Last error: {last_error}")

    def get_token_usage(self) -> dict:
        return dict(self._cumulative_tokens)


if __name__ == "__main__":
    client = LLMClient(
        cache_dir="./.cache/llm",
        token_log_path="./.cache/token_log.jsonl",
    )
    resp = client.call_with_retry(
        messages=[{"role": "user", "content": "Reply with exactly: CLIENT_OK"}],
        model="claude-haiku-4.5",
        max_tokens=10,
    )
    print(f"Test: {resp}")
    print(f"Usage: {client.get_token_usage()}")
