"""
Ollama usage proxy — sits in front of Ollama and logs token counts to the
Token Usage Monitor. Required for cloud models, which never emit eval
counts to the journal.

Default layout on the AI VM:
  Open WebUI  →  :11434 (this proxy)  →  :11435 (real Ollama on localhost)
  On each /api/chat and /api/generate response (stream or not), POST usage
  to http://127.0.0.1:8110/api/usage with source=ollama.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://127.0.0.1:11435").rstrip("/")
TUM_URL = os.environ.get("TUM_USAGE_URL", "http://127.0.0.1:8110/api/usage")
WATCH_PATHS = {"/api/chat", "/api/generate"}

app = FastAPI(title="ClusterCloud Ollama Usage Proxy")
_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup():
    global _client
    _client = httpx.AsyncClient(
        base_url=UPSTREAM,
        timeout=httpx.Timeout(None),  # LLM calls can be long
        follow_redirects=True,
    )


@app.on_event("shutdown")
async def _shutdown():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _report_usage(model: str, tin: int, tout: int) -> None:
    payload = {
        "source": "ollama",
        "model": model or "ollama",
        "input_tokens": int(tin or 0),
        "output_tokens": int(tout or 0),
    }
    try:
        req = urllib.request.Request(
            TUM_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        print(f"[ollama-usage-proxy] report failed: {exc}", file=sys.stderr)


def _maybe_report_from_obj(obj: dict, fallback_model: str = "") -> None:
    if not isinstance(obj, dict):
        return
    # Final chunk / non-stream body carries the counts
    if obj.get("done") is False:
        return
    tin = obj.get("prompt_eval_count")
    tout = obj.get("eval_count")
    if tin is None and tout is None:
        return
    model = obj.get("model") or fallback_model or "ollama"
    # Shorten registry paths if present
    if isinstance(model, str) and "/" in model:
        model = model.rsplit("/", 1)[-1]
    threading.Thread(
        target=_report_usage,
        args=(model, int(tin or 0), int(tout or 0)),
        daemon=True,
    ).start()


def _forward_headers(request: Request) -> dict:
    skip = {"host", "content-length", "connection", "transfer-encoding"}
    return {k: v for k, v in request.headers.items() if k.lower() not in skip}


@app.get("/proxy-health")
def proxy_health():
    return {"ok": True, "upstream": UPSTREAM, "tum": TUM_URL}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    assert _client is not None
    target = "/" + path if path else "/"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    body = await request.body()
    req_model = ""
    want_stream = False
    watch = target.split("?", 1)[0] in WATCH_PATHS and request.method.upper() == "POST"
    if watch and body:
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            req_model = parsed.get("model") or ""
            want_stream = bool(parsed.get("stream"))
        except Exception:
            req_model = ""

    upstream = await _client.request(
        request.method,
        target,
        headers=_forward_headers(request),
        content=body,
    )

    content_type = upstream.headers.get("content-type", "")
    is_stream = (
        want_stream
        or "application/x-ndjson" in content_type
        or "text/event-stream" in content_type
    )

    # Non-streaming body (most Ollama JSON endpoints, and stream=false chat/generate)
    if not is_stream:
        raw = upstream.content
        if watch:
            try:
                _maybe_report_from_obj(
                    json.loads(raw.decode("utf-8", errors="replace")), req_model
                )
            except Exception:
                pass
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=content_type or None,
            headers=_filter_resp_headers(upstream.headers),
        )

    # Streaming NDJSON / SSE — tee lines, report on final done=true
    async def stream() -> AsyncIterator[bytes]:
        buf = b""
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
                if not watch:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(b"data:"):
                        line = line[5:].strip()
                    try:
                        obj = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    _maybe_report_from_obj(obj, req_model)
        finally:
            await upstream.aclose()

    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        media_type=content_type or "application/x-ndjson",
        headers=_filter_resp_headers(upstream.headers),
    )


def _filter_resp_headers(headers: httpx.Headers) -> dict:
    skip = {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
    }
    return {k: v for k, v in headers.items() if k.lower() not in skip}
