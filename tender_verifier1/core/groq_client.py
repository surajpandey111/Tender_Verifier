"""
Cyclic multi-key Groq client.

Why this exists:
  Groq's free tier gives each API key a fairly small requests-per-minute /
  tokens-per-minute budget. With ~400 tenders x ~150 pages, a single key
  will get rate-limited constantly. Instead of paying for a bigger plan,
  we round-robin across N free keys (e.g. 5), so the effective throughput
  is ~N x a single key's limit.

Design rules (per user's requirement):
  - NEVER send a whole document to the LLM. Only ever send ONE page's
    OCR/text-layer text (plus a tightly scoped instruction). This keeps
    token usage tiny and predictable regardless of how large the source
    PDF is.
  - This is a FALLBACK / ENRICHMENT layer, not the primary engine. The
    rule-based classifier and regex extractor must be able to work with
    zero API calls. Groq is only invoked when:
        (a) the rule-based classifier is unsure of a page's document type, or
        (b) a field marked "type": "llm" in document_rules.json needs
            extracting from a page that already got classified.
  - Must run fine unattended on a laptop: no external services besides
    the Groq HTTPS endpoint, no persistent connections, no local GPU.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from groq import Groq, APIStatusError, RateLimitError


@dataclass
class GroqKeyStatus:
    key: str
    client: Groq
    cooldown_until: float = 0.0   # epoch seconds; skip this key until then
    failures: int = 0


class GroqKeyPool:
    """
    Round-robins across a list of Groq API keys. Thread-safe (workers in the
    multiprocessing pool each construct their own GroqKeyPool from env/config,
    so no cross-process locking is needed — only cross-thread within a worker).
    """

    def __init__(self, api_keys: Optional[list[str]] = None, model: str = "llama-3.1-8b-instant"):
        keys = api_keys or self._load_keys_from_env()
        if not keys:
            raise ValueError(
                "No Groq API keys configured. Set GROQ_API_KEYS as a comma-separated "
                "list, or GROQ_API_KEY_1..GROQ_API_KEY_5, or pass api_keys=[...]."
            )
        self._pool: list[GroqKeyStatus] = [GroqKeyStatus(key=k, client=Groq(api_key=k)) for k in keys]
        self._cycle = itertools.cycle(range(len(self._pool)))
        self._lock = threading.Lock()
        self.model = model

    @staticmethod
    def _load_keys_from_env() -> list[str]:
        combined = os.environ.get("GROQ_API_KEYS", "")
        keys = [k.strip() for k in combined.split(",") if k.strip()]
        if keys:
            return keys
        # fallback: GROQ_API_KEY_1 .. GROQ_API_KEY_5
        found = []
        for i in range(1, 6):
            v = os.environ.get(f"GROQ_API_KEY_{i}")
            if v:
                found.append(v)
        return found

    def _next_available(self) -> Optional[GroqKeyStatus]:
        now = time.time()
        with self._lock:
            for _ in range(len(self._pool)):
                idx = next(self._cycle)
                status = self._pool[idx]
                if status.cooldown_until <= now:
                    return status
        return None  # everyone is cooling down

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 500,
        temperature: float = 0.0,
        max_retries: int = 4,
    ) -> Optional[dict]:
        """
        Calls Groq's chat completion asking for a strict JSON response and parses it.
        Returns None (never raises) on total failure — callers must handle None by
        falling back to regex-only / "not extracted" rather than crashing the pipeline.
        This guarantees the pipeline is never *dependent* on the LLM being reachable.
        """
        last_error = None
        for attempt in range(max_retries):
            status = self._next_available()
            if status is None:
                time.sleep(1.5)  # brief pause; all keys cooling down
                continue
            try:
                resp = status.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt + "\nRespond with ONLY valid JSON, no prose, no markdown fences."},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                text = resp.choices[0].message.content.strip()
                text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                return json.loads(text)
            except RateLimitError as e:
                status.cooldown_until = time.time() + 20  # cool this key down, try next
                status.failures += 1
                last_error = e
                continue
            except APIStatusError as e:
                status.failures += 1
                last_error = e
                if e.status_code and e.status_code >= 500:
                    continue  # transient, try next key
                break  # 4xx other than 429 — not worth retrying
            except (json.JSONDecodeError, Exception) as e:  # noqa: BLE001
                last_error = e
                continue

        return None  # total failure — caller falls back gracefully
