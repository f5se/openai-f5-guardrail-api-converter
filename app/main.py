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


def _looks_like_sse_payload(raw: bytes) -> bool:
    sample = raw.lstrip()[:32].lower()
    return sample.startswith(b"data:") or sample.startswith(b"event:") or sample.startswith(b"id:")


def _build_last_user_only_body(original_body: bytes) -> tuple[Optional[bytes], Optional[str]]:
    """
    Keep only the latest role=user message in `messages`, preserve all other fields.
    Returns (new_body, error_message). When error_message is not None, new_body is None.
    """
    try:
        payload = json.loads(original_body.decode("utf-8"))
    except Exception as e:
        return None, f"请求体不是合法 JSON：{e}"

    if not isinstance(payload, dict):
        return None, "请求体必须是 JSON 对象"

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None, "请求体缺少 messages 数组"

    latest_user_msg = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            latest_user_msg = msg
            break

    if latest_user_msg is None:
        return None, "messages 中未找到 role=user 的消息"

    new_payload = dict(payload)
    new_payload["messages"] = [latest_user_msg]
    return json.dumps(new_payload, ensure_ascii=False).encode("utf-8"), None


async def _proxy_chat_completions(request: Request, body_override: Optional[bytes] = None) -> Response:
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

    body = body_override if body_override is not None else await request.body()
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
        if _looks_like_sse_payload(raw):
            payload_sse = raw
        else:
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


def _verify_dify_token(request: Request, expected_token: str) -> Optional[Response]:
    if not expected_token:
        return JSONResponse({"error": {"message": "Server misconfiguration: DIFY_MODERATION_TOKEN is required"}}, status_code=500)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": {"message": "Missing bearer token"}}, status_code=401)
    token = auth[len("Bearer ") :].strip()
    if token != expected_token:
        return JSONResponse({"error": {"message": "Invalid bearer token"}}, status_code=401)
    return None


async def _scan_text_with_f5(text: str) -> tuple[Optional[bool], Optional[str]]:
    settings = get_settings()
    if not settings.f5_scans_url:
        return None, "F5_SCANS_URL 未配置"
    if not settings.f5_scans_api_key:
        return None, "F5_SCANS_API_KEY 未配置"
    assert _client is not None
    try:
        resp = await _client.post(
            settings.f5_scans_url,
            headers={
                "Authorization": f"Bearer {settings.f5_scans_api_key}",
                "Content-Type": "application/json",
            },
            json={"input": text},
        )
    except Exception as e:
        return None, f"调用 scans 接口失败：{e}"

    if resp.status_code != 200:
        try:
            err_text = (await resp.aread()).decode("utf-8", errors="ignore")
        except Exception:
            err_text = ""
        return None, f"scans 接口返回非 200：{resp.status_code} {err_text[:200]}"

    try:
        data = resp.json()
    except Exception as e:
        return None, f"scans 响应 JSON 解析失败：{e}"

    result = data.get("result") if isinstance(data, dict) else None
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if not isinstance(outcome, str):
        return None, "scans 响应缺少 result.outcome"
    return outcome.lower() == "flagged", None


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await _proxy_chat_completions(request)


@app.post("/last/v1/chat/completions")
@app.post("/last/chat/completions")
async def chat_completions_last_user_only(request: Request) -> Response:
    original_body = await request.body()
    rewritten_body, error = _build_last_user_only_body(original_body)
    if error is not None:
        return JSONResponse(
            {
                "error": {
                    "message": f"/last 路由请求转换失败：{error}",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )

    assert rewritten_body is not None
    return await _proxy_chat_completions(request, body_override=rewritten_body)


@app.post("/dify/moderation")
@app.post("/moderation")
async def dify_moderation(request: Request) -> Response:
    settings = get_settings()
    auth_error = _verify_dify_token(request, settings.dify_moderation_token)
    if auth_error is not None:
        return auth_error

    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse(
            {"error": {"message": f"请求体不是合法 JSON：{e}", "type": "invalid_request_error"}},
            status_code=400,
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )

    point = payload.get("point")
    params = payload.get("params")
    if point == "ping":
        return JSONResponse({"result": "pong"}, status_code=200)
    if not isinstance(params, dict):
        return JSONResponse(
            {"error": {"message": "缺少 params 对象", "type": "invalid_request_error"}},
            status_code=400,
        )

    if point == "app.moderation.input":
        content = params.get("query")
    elif point == "app.moderation.output":
        content = params.get("text")
    else:
        return JSONResponse(
            {
                "error": {
                    "message": "不支持的 point，仅支持 app.moderation.input/app.moderation.output/ping",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )

    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)

    # 空文本按未命中处理，避免对 scans 做无意义调用。
    if not content.strip():
        return JSONResponse({"flagged": False}, status_code=200)

    flagged, err = await _scan_text_with_f5(content)
    if err is not None:
        # 审查服务异常时，保守处理为拦截，防止漏检。
        return JSONResponse(
            {
                "flagged": True,
                "action": "direct_output",
                "preset_response": settings.moderation_block_message,
                "error": err,
            },
            status_code=200,
        )

    if flagged:
        return JSONResponse(
            {
                "flagged": True,
                "action": "direct_output",
                "preset_response": settings.moderation_block_message,
            },
            status_code=200,
        )

    return JSONResponse({"flagged": False}, status_code=200)
