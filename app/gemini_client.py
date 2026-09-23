"""Gemini wrapper: model selection with fallback, retries, structured output, caching, token/cost logging.

Never sends secrets or Meera's Telegram ID to the model. Callers pass note text and context only.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

TRANSCRIBE_INSTRUCTION = (
    "Produce a verbatim transcript of this voice note. Keep technical terms such as pH, INCI, CoA, "
    "niacinamide, ceramide and batch numbers exactly as spoken. Do not summarise, correct or add anything. "
    "Return only the transcript text."
)


class GeminiAuthError(RuntimeError):
    pass


class GeminiUnavailable(RuntimeError):
    pass


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code == 429 or (exc.code or 0) >= 500
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)) or exc.__class__.__name__ in {
        "ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"}


@dataclass
class CallResult:
    data: Any
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    raw_text: str = ""


@dataclass
class _Cache:
    name: str
    expires: float


@dataclass
class Gemini:
    settings: Settings
    db: DB
    model: str = ""
    client: Any = None
    _caches: dict[str, _Cache] = field(default_factory=dict)
    _cache_disabled: bool = False

    def __post_init__(self) -> None:
        # Reuse the model picked by the last health check (serverless: don't re-list models on every request).
        saved = (self.db.get_kv("gemini_model") or "").split("|")
        self.model = saved[1] if len(saved) == 2 and saved[0] == self.settings.gemini_model else self.settings.gemini_model
        if self.client is None:
            self.client = genai.Client(api_key=self.settings.gemini_api_key,
                                       http_options=types.HttpOptions(timeout=60_000))

    # ---- startup ----------------------------------------------------------
    async def health_check(self) -> str:
        """List models, pick GEMINI_MODEL or a fallback, and make one tiny call. Returns chosen model."""
        try:
            names = []
            pager = await self.client.aio.models.list()
            async for m in pager:
                names.append(m.name.removeprefix("models/"))
        except errors.APIError as e:
            if e.code in (400, 401, 403):
                raise GeminiAuthError(self._auth_msg(e)) from e
            raise GeminiUnavailable(f"Could not list Gemini models: {e.code} {e.status}") from e

        for candidate in [self.settings.gemini_model, *self.settings.fallback_models]:
            if candidate in names:
                self.model = candidate
                break
        else:
            flash = sorted(n for n in names if "flash" in n and "2.5" not in n and "tts" not in n
                           and "image" not in n and "live" not in n)
            raise GeminiUnavailable(
                f"None of {self.settings.gemini_model}, {self.settings.fallback_models} are available to this key. "
                f"Available Flash models: {flash[:10]}. Update GEMINI_MODEL in .env.")
        if self.model != self.settings.gemini_model:
            log.warning("GEMINI_MODEL %s not available; using fallback %s", self.settings.gemini_model, self.model)
        self.db.set_kv("gemini_model", f"{self.settings.gemini_model}|{self.model}")

        try:
            await self.client.aio.models.generate_content(model=self.model, contents="Reply with the word OK.")
        except errors.APIError as e:
            if e.code in (400, 401, 403):
                raise GeminiAuthError(self._auth_msg(e)) from e
            raise GeminiUnavailable(f"Gemini test call failed: {e.code} {e.status}") from e
        log.info("Gemini ready: model=%s", self.model)
        return self.model

    @staticmethod
    def _auth_msg(e: errors.APIError) -> str:
        return (f"Gemini key rejected: {e.code} {e.status} {e.message}. New AI Studio keys start with AQ. and are "
                "valid; check the key was pasted completely on one line, without spaces or line breaks.")

    # ---- core call --------------------------------------------------------
    def _cost(self, tin: int, tout: int) -> float:
        return tin / 1e6 * self.settings.price_input_per_m + tout / 1e6 * self.settings.price_output_per_m

    async def _system_cache(self, system: str) -> str | None:
        """Cache long system instructions (the ~12k-token voice skill). Returns cache name or None."""
        if self._cache_disabled or len(system) < 20_000:
            return None
        key = f"{self.model}:{hashlib.sha256(system.encode()).hexdigest()[:16]}"
        c = self._caches.get(key)
        if c is None:  # another serverless invocation may already have created it
            saved = (self.db.get_kv(f"gemini_cache:{key}") or "").split("|")
            if len(saved) == 2:
                c = _Cache(saved[0], float(saved[1]))
        if c and c.expires > time.time() + 60:
            return c.name
        try:
            cache = await self.client.aio.caches.create(
                model=self.model,
                config=types.CreateCachedContentConfig(system_instruction=system, ttl="3600s",
                                                       display_name="skinstinct-voice"))
            self._caches[key] = _Cache(cache.name, time.time() + 3600)
            self.db.set_kv(f"gemini_cache:{key}", f"{cache.name}|{time.time() + 3600}")
            return cache.name
        except Exception as e:  # caching is an optimisation only
            log.info("Context caching unavailable (%s); sending system instruction inline.", e.__class__.__name__)
            self._cache_disabled = True
            return None

    async def _generate(self, *, purpose: str, contents: Any, system: str | None, temperature: float,
                        schema: type[BaseModel] | None) -> Any:
        cache_name = await self._system_cache(system) if system else None
        cfg: dict[str, Any] = {"temperature": temperature}
        if cache_name:
            cfg["cached_content"] = cache_name
        elif system:
            cfg["system_instruction"] = system
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = schema

        t0 = time.perf_counter()
        try:
            async for attempt in AsyncRetrying(retry=retry_if_exception(_retryable), reraise=True,
                                               wait=wait_random_exponential(multiplier=1, max=30),
                                               stop=stop_after_attempt(4)):
                with attempt:
                    resp = await asyncio.wait_for(
                        self.client.aio.models.generate_content(
                            model=self.model, contents=contents, config=types.GenerateContentConfig(**cfg)),
                        timeout=90)
        except errors.APIError as e:
            if cache_name and e.code in (400, 403, 404):  # cache expired or unsupported: retry without it
                self._cache_disabled = True
                self.db.run("DELETE FROM kv WHERE key LIKE ?", ("gemini_cache:%",))
                return await self._generate(purpose=purpose, contents=contents, system=system,
                                            temperature=temperature, schema=schema)
            self.db.llm_log(purpose=purpose, model=self.model, ok=False, error=f"{e.code} {e.status}",
                            latency_ms=int((time.perf_counter() - t0) * 1000))
            if e.code in (401, 403):
                raise GeminiAuthError(self._auth_msg(e)) from e
            raise GeminiUnavailable(f"Gemini error {e.code} {e.status}") from e
        except Exception as e:
            self.db.llm_log(purpose=purpose, model=self.model, ok=False, error=e.__class__.__name__,
                            latency_ms=int((time.perf_counter() - t0) * 1000))
            raise GeminiUnavailable(f"Gemini call failed: {e.__class__.__name__}") from e

        um = getattr(resp, "usage_metadata", None)
        tin = (getattr(um, "prompt_token_count", 0) or 0) if um else 0
        tout = (getattr(um, "candidates_token_count", 0) or 0) if um else 0
        cost = self._cost(tin, tout)
        self.db.llm_log(purpose=purpose, model=self.model, tokens_in=tin, tokens_out=tout, cost_est=cost,
                        latency_ms=int((time.perf_counter() - t0) * 1000), ok=True)
        return resp, tin, tout, cost

    async def generate_json(self, *, purpose: str, prompt: str, schema: type[T], system: str | None = None,
                            temperature: float = 0.2) -> CallResult:
        """Structured call validated against a Pydantic schema; one repair retry on parse failure."""
        contents = prompt
        total_in = total_out = 0
        total_cost = 0.0
        for attempt in range(2):
            resp, tin, tout, cost = await self._generate(purpose=purpose, contents=contents, system=system,
                                                         temperature=temperature, schema=schema)
            total_in += tin
            total_out += tout
            total_cost += cost
            text = resp.text or ""
            try:
                parsed = resp.parsed if isinstance(getattr(resp, "parsed", None), schema) else \
                    schema.model_validate_json(text)
                return CallResult(parsed, self.model, total_in, total_out, total_cost, text)
            except (ValidationError, ValueError) as e:
                if attempt == 1:
                    raise GeminiUnavailable(f"Gemini returned invalid JSON for {purpose}: {e}") from e
                contents = (f"{prompt}\n\nYour previous response failed validation with this error:\n{e}\n"
                            "Return corrected JSON that matches the schema exactly.")
        raise AssertionError("unreachable")

    async def generate_text(self, *, purpose: str, contents: Any, system: str | None = None,
                            temperature: float = 0.2) -> CallResult:
        resp, tin, tout, cost = await self._generate(purpose=purpose, contents=contents, system=system,
                                                     temperature=temperature, schema=None)
        return CallResult((resp.text or "").strip(), self.model, tin, tout, cost, resp.text or "")

    async def transcribe(self, audio: bytes, mime_type: str = "audio/ogg") -> str:
        res = await self.generate_text(
            purpose="transcribe",
            contents=[types.Part.from_bytes(data=audio, mime_type=mime_type), TRANSCRIBE_INSTRUCTION],
            temperature=0.0)
        return res.data
