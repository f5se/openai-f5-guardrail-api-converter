from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import get_settings
from app.guardrail import (
    build_blocked_assistant_text,
    chat_completion_json,
    extract_failed_scanners,
    parse_client_body,
    parse_error_fallback_text,
    sse_chunks_for_blocked,
)

_client: Optional[httpx.AsyncClient] = None


def _build_http_client() -> httpx.AsyncClient:
    s = get_settings()
    timeout = httpx.Timeout(
        connect=s.http_timeout_connect,
        read=s.http_timeout_read,
        write=s.http_timeout_write,
        pool=s.http_timeout_pool,
    )
    limits = httpx.Limits(
        max_connections=s.max_connections,
        max_keepalive_connections=s.max_keepalive_connections,
    )
    return httpx.AsyncClient(timeout=timeout, limits=limits)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = _build_http_client()
    try:
        yield
    finally:
        if _client is not None:
            await _client.aclose()
            _client = None


app = FastAPI(title="OpenAI–F5 Guardrail proxy", lifespan=lifespan)


def _upstream_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    auth = request.headers.get("authorization")
    if auth:
        out["Authorization"] = auth
    ct = request.headers.get("content-type")
    if ct:
        out["Content-Type"] = ct
    else:
        out["Content-Type"] = "application/json"
    return out


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
@app.get("/models")
async def list_models() -> JSONResponse:
    s = get_settings()
    created = 1677610602
    payload = {
        "object": "list",
        "data": [
            {
                "id": s.models_list_id,
                "object": "model",
                "created": created,
                "owned_by": s.models_list_owned_by,
            }
        ],
    }
    return JSONResponse(payload)


def _parse_guardrail_400(raw: bytes) -> str:
    try:
        text_body = raw.decode("utf-8")
    except Exception:
        return parse_error_fallback_text("无法将阻断响应解码为 UTF-8 文本")

    payload: Optional[dict] = None
    try:
        parsed = json.loads(text_body)
        if isinstance(parsed, dict):
            payload = parsed
    except Exception as e:
        return parse_error_fallback_text(f"JSON 解析失败：{e}")

    if payload is None:
        return parse_error_fallback_text("响应体不是合法 JSON")

    failed = extract_failed_scanners(payload)
    if failed is None:
        return parse_error_fallback_text("缺少 error.cai_error.scanner_results 结构或格式无效")
    return build_blocked_assistant_text(failed)


async def _proxy_chat_completions(request: Request) -> Response:
    settings = get_settings()
    if not settings.upstream_base_url:
        return JSONResponse(
            {
                "error": {
                    "message": "Server misconfiguration: UPSTREAM_BASE_URL is not set",
                    "type": "invalid_request_error",
                }
            },
            status_code=500,
        )

    body = await request.body()
    stream_requested, model_name = parse_client_body(body)
    headers = _upstream_headers(request)

    assert _client is not None

    if not stream_requested:
        upstream = await _client.post(
            settings.upstream_base_url,
            headers=headers,
            content=body,
        )
        status_code = upstream.status_code
        ct = upstream.headers.get("content-type", "application/json")
        media = ct.split(";")[0].strip() if ct else "application/json"

        if status_code == 200:
            content = await upstream.aread()
            return Response(content=content, status_code=200, media_type=media)

        raw = await upstream.aread()
        if status_code == 400:
            content_text = _parse_guardrail_400(raw)
            return JSONResponse(
                chat_completion_json(content=content_text, model=model_name),
                status_code=200,
            )
        return Response(content=raw, status_code=status_code, media_type=media)

    async with _client.stream(
        "POST",
        settings.upstream_base_url,
        headers=headers,
        content=body,
    ) as probe:
        sc = probe.status_code
        pct = probe.headers.get("content-type", "")

        if sc == 200 and "text/event-stream" in pct.lower():

            async def passthrough_sse() -> AsyncIterator[bytes]:
                async with _client.stream(
                    "POST",
                    settings.upstream_base_url,
                    headers=headers,
                    content=body,
                ) as upstream:
                    async for chunk in upstream.aiter_raw():
                        yield chunk

            return StreamingResponse(
                passthrough_sse(),
                status_code=200,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        raw = await probe.aread()

    if sc == 400:
        content_text = _parse_guardrail_400(raw)

        async def blocked_sse() -> AsyncIterator[bytes]:
            for frame in sse_chunks_for_blocked(content=content_text, model=model_name):
                yield frame.encode("utf-8")

        return StreamingResponse(
            blocked_sse(),
            status_code=200,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    if sc == 200:
        media = pct.split(";")[0].strip() if pct else "application/json"
        line = raw.strip()
        payload_sse = b"data: " + line + b"\n\n" + b"data: [DONE]\n\n"
        return StreamingResponse(
            _single_chunk(payload_sse),
            status_code=200,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    media_err = pct.split(";")[0].strip() if pct else "application/json"
    return Response(content=raw, status_code=sc, media_type=media_err)


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await _proxy_chat_completions(request)
