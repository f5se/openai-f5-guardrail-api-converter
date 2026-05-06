from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional, Tuple

BLOCK_HEADER = "请求已被F5 Guardrail由以下策略阻断："


def extract_failed_scanners(payload: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    """
    Returns list of dicts with scanner_id and message for outcome=='failed',
    in original array order. None if structure is missing or invalid.
    """
    try:
        err = payload.get("error")
        if not isinstance(err, dict):
            return None
        cai = err.get("cai_error")
        if not isinstance(cai, dict):
            return None
        results = cai.get("scanner_results")
        if not isinstance(results, list):
            return None
        failed: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("outcome") != "failed":
                continue
            sid = item.get("scanner_id")
            if sid is None:
                continue
            failed.append({"scanner_id": str(sid), "message": item.get("message")})
        return failed
    except Exception:
        return None


def build_blocked_assistant_text(failed_scanners: list[dict[str, Any]]) -> str:
    lines = [BLOCK_HEADER]
    for row in failed_scanners:
        lines.append(str(row["scanner_id"]))
        msg = row.get("message")
        if msg is not None and str(msg).strip() != "":
            lines.append(str(msg))
    return "\n".join(lines)


def parse_client_body(body: bytes) -> Tuple[bool, str]:
    """
    Returns (stream_requested, model_name).
    Defaults: stream False, model empty string.
    """
    stream_requested = False
    model_name = ""
    if not body:
        return stream_requested, model_name
    try:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict):
            stream_requested = bool(data.get("stream"))
            m = data.get("model")
            if isinstance(m, str):
                model_name = m
    except Exception:
        pass
    return stream_requested, model_name


def chat_completion_json(
    *,
    content: str,
    model: str,
    completion_id: Optional[str] = None,
) -> dict[str, Any]:
    cid = completion_id or str(uuid.uuid4())

    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def parse_error_fallback_text(reason: str) -> str:
    return (
        "F5 Guardrail 返回阻断响应，但中间服务未能按预期解析详情。\n"
        f"异常原因：{reason}"
    )


def sse_chunks_for_blocked(*, content: str, model: str) -> list[str]:
    """Returns SSE frame payloads (full lines including 'data: ' prefix without trailing double newline handled here)."""
    completion_id = str(uuid.uuid4())
    created = int(time.time())
    base = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model or "unknown",
    }

    chunks: list[str] = []

    first = {
        **base,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ],
    }
    chunks.append("data: " + json.dumps(first, ensure_ascii=False) + "\n\n")

    second = {
        **base,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }
    chunks.append("data: " + json.dumps(second, ensure_ascii=False) + "\n\n")

    final = {
        **base,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    chunks.append("data: " + json.dumps(final, ensure_ascii=False) + "\n\n")
    chunks.append("data: [DONE]\n\n")
    return chunks
