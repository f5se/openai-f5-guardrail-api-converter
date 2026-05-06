import os
from dataclasses import dataclass
from functools import lru_cache


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    host: str
    port: int
    http_timeout_connect: float
    http_timeout_read: float
    http_timeout_write: float
    http_timeout_pool: float
    max_connections: int
    max_keepalive_connections: int
    models_list_id: str
    models_list_owned_by: str
    f5_scans_url: str
    f5_scans_api_key: str
    dify_moderation_token: str
    moderation_input_block_message: str
    moderation_output_block_message: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", "").strip(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8080),
        http_timeout_connect=_env_float("HTTP_TIMEOUT_CONNECT", 30.0),
        http_timeout_read=_env_float("HTTP_TIMEOUT_READ", 600.0),
        http_timeout_write=_env_float("HTTP_TIMEOUT_WRITE", 60.0),
        http_timeout_pool=_env_float("HTTP_TIMEOUT_POOL", 5.0),
        max_connections=_env_int("MAX_CONNECTIONS", 200),
        max_keepalive_connections=_env_int("MAX_KEEPALIVE_CONNECTIONS", 50),
        models_list_id=os.environ.get("MODELS_LIST_ID", "placeholder-model"),
        models_list_owned_by=os.environ.get("MODELS_LIST_OWNED_BY", "proxy"),
        f5_scans_url=os.environ.get("F5_SCANS_URL", "").strip(),
        f5_scans_api_key=os.environ.get("F5_SCANS_API_KEY", "").strip(),
        dify_moderation_token=os.environ.get("DIFY_MODERATION_TOKEN", "").strip(),
        moderation_input_block_message=os.environ.get(
            "MODERATION_INPUT_BLOCK_MESSAGE",
            "请求经F5 Guardrail检查存在违规。",
        ),
        moderation_output_block_message=os.environ.get(
            "MODERATION_OUTPUT_BLOCK_MESSAGE",
            "响应经F5 Guardrail检查存在违规。",
        ),
    )
