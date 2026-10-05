"""Model routing: a prose tier and a utility tier.

prose()    streams narration — the thing you actually read.
utility()  makes ONE structured call per turn and returns validated JSON.

The utility call uses provider-native structured output where available, so the
memory pipeline never parses JSON out of prose and never retries on malformed
output. See MEASUREMENTS.md.

stdlib only — urllib, no requests, matching the rest of the project.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator, Optional

import config

OLLAMA_CHAT = "/api/chat"

TIMEOUT = 180        # generation can legitimately take minutes
EMBED_TIMEOUT = 10   # embeddings and health probes must fail fast


class BrainError(RuntimeError):
    pass


# Faults that are worth trying again: the provider is busy, not the request wrong.
_RETRYABLE = ("overloaded", "rate_limit", "rate limit", "429", "500", "502", "503", "529",
              "api_error", "timeout", "timed out", "temporarily", "empty stream",
              "stream ended early", "connection reset")


def _retryable(e: Optional[BaseException]) -> bool:
    return bool(e) and any(t in str(e).lower() for t in _RETRYABLE)


def _post(url: str, payload: dict, headers: dict, *, stream: bool = False, timeout: int = TIMEOUT):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise BrainError(f"{e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise BrainError(f"cannot reach {url}: {e.reason}") from e
    return resp if stream else json.loads(resp.read().decode("utf-8"))


def _sse_lines(resp) -> Iterator[dict]:
    """Yield decoded `data:` payloads from an SSE stream."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


# ---------------------------------------------------------------- prose


def _prose_openai_compat(spec: dict, system: str, messages: list[dict], url: str, key: str) -> Iterator[str]:
    payload = {
        "model": spec["model"],
        "max_tokens": spec["max_tokens"],
        "temperature": spec.get("temperature", 1.0),
        "stream": True,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    resp = _post(url, payload, headers, stream=True)
    for ev in _sse_lines(resp):
        for ch in ev.get("choices", []):
            delta = ch.get("delta") or {}
            # Reasoning models stream their scratchpad first, and can do so
            # for over a minute with long silent gaps. Pass it up so the turn
            # shows progress and the socket keeps moving. See MEASUREMENTS.md.
            if delta.get("reasoning_content"):
                yield "thinking", delta["reasoning_content"]
            piece = delta.get("content")
            if piece:
                yield "content", piece
            # OpenAI-shaped APIs call it finish_reason "length"; normalised
            # to "max_tokens". See MEASUREMENTS.md.
            fin = ch.get("finish_reason")
            if fin:
                yield "stop", "max_tokens" if fin == "length" else fin


def _ndjson_lines(resp) -> Iterator[dict]:
    """Yield decoded objects from a newline-delimited JSON stream (ollama)."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _ollama_options(spec: dict) -> dict:
    """Sampler options, omitting every one left at its disabled default.

    Sent sparsely on purpose: ollama merges these over the Modelfile's own
    parameters, so passing top_k=0 would override a model's tuned default with
    "off" rather than leaving it alone.
    """
    opts: dict[str, Any] = {
        "temperature": spec.get("temperature", 1.0),
        "num_predict": spec["max_tokens"],
        "num_ctx": spec.get("num_ctx", 8192),
    }
    if (v := spec.get("top_p", 1.0)) < 1.0:
        opts["top_p"] = v
    if (v := spec.get("top_k", 0)):
        opts["top_k"] = v
    if (v := spec.get("min_p", 0.0)):
        opts["min_p"] = v
    if (v := spec.get("repeat_penalty", 1.0)) != 1.0:
        opts["repeat_penalty"] = v
        opts["repeat_last_n"] = spec.get("repeat_last_n", 64)
    return opts


def _prose_ollama(spec: dict, system: str, messages: list[dict], url: str) -> Iterator[str]:
    payload = {
        "model": spec["model"],
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": True,
        "options": _ollama_options(spec),
    }
    resp = _post(url.rstrip("/") + OLLAMA_CHAT, payload,
                 {"content-type": "application/json"}, stream=True)
    for ev in _ndjson_lines(resp):
        if err := ev.get("error"):
            raise BrainError(f"ollama: {err}")
        piece = (ev.get("message") or {}).get("content")
        if piece:
            yield "content", piece
        if ev.get("done"):
            # ollama says "length"; normalised to "max_tokens", which the
            # turn loop keys the resume button off. See MEASUREMENTS.md.
            reason = ev.get("done_reason") or "stop"
            yield "stop", "max_tokens" if reason == "length" else reason


def prose(
    system: str,
    messages: list[dict],
    *,
    on_token: Optional[Callable[[str], None]] = None,
    on_thinking: Optional[Callable[[int], None]] = None,
    on_retry: Optional[Callable[[int, float, str], None]] = None,
    on_stop: Optional[Callable[[str], None]] = None,
    spec: Optional[dict] = None,
) -> str:
    """Stream a narration turn. Returns the full text; calls on_token per chunk.

    on_thinking is called with the running reasoning-character count while a
    reasoning model deliberates, before any visible text exists.

    on_stop is called once with the provider's stop reason for the attempt that
    actually produced text — "max_tokens" is the one that matters, because it
    means the reply is unfinished rather than over.
    """
    spec = spec or config.PROSE

    def _stream(current: dict):
        provider = current.get("provider", "ollama")
        if provider == "ollama":
            return _prose_ollama(
                current, system, messages, current.get("url") or config.OLLAMA_URL)
        if provider == "openai_compat":
            url = current.get("url") or ""
            if not url:
                raise BrainError("openai_compat provider needs a 'url'")
            return _prose_openai_compat(
                current, system, messages, url.rstrip("/") + "/chat/completions", "")
        raise BrainError(f"unknown prose provider: {provider}")

    # Retry transient provider faults, but ONLY while nothing has been shown yet:
    # once tokens have reached the browser, a retry would duplicate half a reply.
    last: Optional[BrainError] = None
    for attempt in range(max(0, config.PROSE_RETRIES) + 1):
        out: list[str] = []
        thought = 0
        stop_reason = ""
        try:
            for kind, piece in _stream(spec):
                if not piece:
                    continue
                if kind == "thinking":
                    thought += len(piece)
                    if on_thinking:
                        on_thinking(thought)
                    continue
                if kind == "stop":
                    stop_reason = piece
                    continue
                out.append(piece)
                if on_token:
                    on_token(piece)
        except BrainError as e:
            if out:
                raise
            last = e
        else:
            text = "".join(out)
            if text.strip():
                if stop_reason and on_stop:
                    on_stop(stop_reason)
                return text
            last = BrainError(
                f"{spec.get('provider')}/{spec.get('model')} returned no text"
                + (f" after {thought} characters of reasoning (stream ended early)"
                   if thought else " (empty stream)")
            )

        if attempt < config.PROSE_RETRIES and _retryable(last):
            delay = config.PROSE_RETRY_BACKOFF * (2 ** attempt)
            print(f"[prose] {last} — retrying in {delay:.0f}s "
                  f"({attempt + 1}/{config.PROSE_RETRIES})")
            # Its own callback, not on_thinking. Reusing the reasoning channel
            # made a provider outage display as "reasoning…", which told the
            # reader the model was working when it was in fact unavailable.
            if on_retry:
                on_retry(attempt + 1, delay, str(last))
            time.sleep(delay)
            continue
        raise last
    raise last or BrainError("prose failed")


# ---------------------------------------------------------------- utility


def utility(prompt: str, schema: dict, *, spec: Optional[dict] = None) -> dict:
    """One structured call. Returns a dict matching `schema`.

    Uses provider-native structured output so the result is guaranteed parseable
    rather than hoped-for.
    """
    spec = spec or config.UTILITY
    provider = spec.get("provider", "ollama")

    if provider == "ollama":
        # ollama constrains generation to a JSON schema with `format`. Measured
        # on Cydonia 24B IQ3_M: 10/10 valid and schema-conforming, ~8s a call.
        url = (spec.get("url") or config.OLLAMA_URL).rstrip("/") + OLLAMA_CHAT
        data = _post(url, {
            "model": spec["model"], "stream": False, "format": schema,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": spec.get("temperature", 0.3),
                        "num_ctx": spec.get("num_ctx", 8192),
                        "num_predict": spec.get("max_tokens", 1500)},
        }, {"content-type": "application/json"})
        body = (data.get("message") or {}).get("content") or ""
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise BrainError(f"ollama utility returned non-JSON: {body[:200]}") from e

    # OpenAI-compatible providers (local) — ask for a JSON object.
    url = spec.get("url", "").rstrip("/") + "/chat/completions"
    key = ""
    payload = {
        "model": spec["model"],
        "max_tokens": spec["max_tokens"],
        "temperature": spec.get("temperature", 0.3),
        # Strict json_schema, not json_object: json_object honours "give me
        # JSON" and nothing else, and has been measured silently dropping half a
        # delta. Fall back only if the provider rejects the parameter outright.
        # See MEASUREMENTS.md.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "delta", "schema": schema, "strict": True},
        },
        "messages": [
            {
                "role": "system",
                "content": "Reply with a single JSON object matching this schema and nothing else:\n"
                + json.dumps(schema),
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    try:
        data = _post(url, payload, headers)
    except BrainError as e:
        # Local llama.cpp/KoboldCpp servers often implement json_object only.
        if "json_schema" not in str(e) and "response_format" not in str(e):
            raise
        payload["response_format"] = {"type": "json_object"}
        data = _post(url, payload, headers)
    text = data["choices"][0]["message"]["content"]
    return json.loads(text)


# ---------------------------------------------------------------- embeddings


def embed(texts: list[str]) -> list[list[float]]:
    """Embeddings via Ollama. Returns [] per text on failure — callers degrade
    to recency ordering rather than breaking the turn."""
    if not config.EMBED_ENABLED or not texts:
        return [[] for _ in texts]
    out: list[list[float]] = []
    url = config.EMBED_URL.rstrip("/") + "/api/embeddings"
    for t in texts:
        try:
            data = _post(
                url,
                {"model": config.EMBED_MODEL, "prompt": t},
                {"content-type": "application/json"},
                timeout=EMBED_TIMEOUT,
            )
            out.append([float(x) for x in data.get("embedding", [])])
        except (BrainError, KeyError, ValueError, OSError):
            # OSError covers the socket-level TimeoutError, which _post does not
            # wrap. Without it this function broke the promise in its own
            # docstring: a slow embedder took the whole turn down instead of
            # degrading to recency ordering. That is rare against a warm,
            # dedicated ollama and routine against one that is also loading a
            # 10GB prose model — i.e. exactly the local-prose setup.
            out.append([])
    return out


# ---------------------------------------------------------------- model lists


MODELS_TTL = 300.0
_models_cache: dict[str, tuple[float, dict]] = {}


def _get(url: str, headers: dict, timeout: int = EMBED_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise BrainError(f"{e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise BrainError(f"cannot reach {url}: {e.reason}") from e
    return json.loads(resp.read().decode("utf-8"))


def models_list(provider: str, *, force: bool = False) -> dict:
    """Ask the provider which models it will actually accept.

    Returns {"models": [...], "error": str|None}. An empty list with an error is
    a normal, expected result — no key configured, no network, a provider with no
    listing endpoint — and callers must treat it as "cannot verify", never as
    "no valid models exist". Refusing to save a model name because we could not
    reach an API would be worse than not checking at all.
    """
    import time

    hit = _models_cache.get(provider)
    if hit and not force and time.time() - hit[0] < MODELS_TTL:
        return hit[1]

    out: dict[str, Any] = {"provider": provider, "models": [], "error": None}
    try:
        if provider == "ollama":
            # Native ollama lists under /api/tags with a different shape.
            tags = _get(config.OLLAMA_URL.rstrip("/") + "/api/tags", {})
            data = {"data": [{"id": m.get("name")} for m in tags.get("models", [])]}
        elif provider == "openai_compat":
            # PROSE.url first: LOCAL is the optional third tier and is usually
            # unset, which is what made this report "no local URL configured"
            # for a provider that was configured perfectly well.
            url = (config.PROSE.get("url") or config.LOCAL.get("url") or "").rstrip("/")
            if not url:
                raise BrainError("no base URL configured for openai_compat")
            data = _get(url + "/models", {})
        else:
            raise BrainError(f"unknown provider: {provider}")

        ids = sorted({m.get("id") for m in data.get("data", []) if m.get("id")})
        out["all"] = ids
        # These listings mix modalities — an ollama install holds embedding
        # models alongside the chat ones. Both knobs that use this list are
        # text generators, so offer text models and keep the rest in "all".
        # Name-based and therefore fallible, which is why nothing is *rejected*
        # on this basis; it only decides menu order.
        skip = ("imagine", "image", "video", "embed", "tts", "whisper", "audio", "rerank")
        out["models"] = [m for m in ids if not any(s in m.lower() for s in skip)]
        out["hidden"] = [m for m in ids if m not in out["models"]]
    except (BrainError, KeyError, ValueError, TypeError) as e:
        out["error"] = str(e)

    _models_cache[provider] = (time.time(), out)
    return out


def probe(provider: str, model: str, *, timeout: int = 25) -> dict:
    """Send the smallest possible real request and report what came back.

    A model listing says what exists, not what is answering — during the Opus 5
    incident the model was listed and returned 529 to every call. Only an actual
    request distinguishes the two.
    """
    t0 = time.time()
    try:
        if provider == "ollama":
            base = (config.PROSE.get("url") or config.OLLAMA_URL).rstrip("/")
            _post(base + OLLAMA_CHAT,
                  {"model": model, "stream": False,
                   "messages": [{"role": "user", "content": "ok"}],
                   "options": {"num_predict": 4}},
                  {"content-type": "application/json"}, timeout=timeout)
            return {"model": model, "ok": True, "state": "up",
                    "ms": int((time.time() - t0) * 1000), "error": None,
                    "code": 200}
        base = (config.PROSE.get("url") or config.LOCAL.get("url") or "").rstrip("/")
        if not base:
            raise BrainError("no base URL configured for openai_compat")
        _post(base + "/chat/completions",
              {"model": model, "max_tokens": 8,
               "messages": [{"role": "user", "content": "ok"}]},
              {"content-type": "application/json"}, timeout=timeout)
        return {"model": model, "ok": True, "state": "up",
                "ms": int((time.time() - t0) * 1000), "error": None, "code": 200}
    except BrainError as e:
        msg = str(e)
        code = 0
        head = msg.split(" ", 1)[0]
        if head.isdigit():
            code = int(head)
        # Three outcomes, not two. A 529 means come back later; a 400 means this
        # model will never serve this request shape and switching to it would not
        # help. Reporting both as "down" sends you chasing the wrong fix.
        if _retryable(e) or code in (408, 409, 429) or code >= 500:
            state = "busy"
        else:
            state = "unusable"
        return {"model": model, "ok": False, "state": state,
                "ms": int((time.time() - t0) * 1000),
                "error": msg[:200], "code": code, "transient": _retryable(e)}


def probe_all(provider: str, models: Optional[list[str]] = None) -> list[dict]:
    """Probe several models at once. Concurrent, or this is unusably slow."""
    from concurrent.futures import ThreadPoolExecutor

    if models is None:
        models = models_list(provider).get("models", [])
    models = models[:12]          # bounded: each probe is a real billed request
    if not models:
        return []
    with ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(lambda m: probe(provider, m), models))


def health() -> dict:
    """Cheap reachability probe for the UI."""
    status = {"prose": config.PROSE["model"], "utility": config.UTILITY["model"],
              "prose_provider": config.PROSE["provider"],
              "utility_provider": config.UTILITY["provider"]}
    try:
        v = embed(["ping"])[0]
        status["embeddings"] = len(v) if v else 0
    except Exception:  # pragma: no cover - probe only
        status["embeddings"] = 0
    return status


# Kept as the public name used by settings.py and the /api/models route.
models = models_list
