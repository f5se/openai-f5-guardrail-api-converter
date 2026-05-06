# OpenAI ↔ F5 Guardrail 中间代理

在客户端与 F5 Guardrail（OpenAI 兼容上游）之间转发 `chat/completions` 请求。上游正常返回时原样透传；上游以 HTTP 400 返回 Guardrail 阻断详情时，转换为 **HTTP 200** 下的 OpenAI 兼容响应（含非流式 JSON 或 SSE 流），便于仅支持 OpenAI 形态的客户端消费。

## 功能概要

- **接收**：`POST /v1/chat/completions`（另支持 `POST /chat/completions`），请求体与 OpenAI Chat Completions 一致。
- **接收（last 模式）**：`POST /last/v1/chat/completions`（另支持 `POST /last/chat/completions`），会将请求体中的 `messages` 裁剪为仅保留最新一条 `role=user` 消息后再转发上游，其它字段保持不变。
- **转发**：将收到的请求体与 `Authorization`、`Content-Type` 等必要头转发至配置的完整上游 URL（含路径）。
- **成功**：上游 HTTP 200 时，响应体原样返回；客户端请求 `stream: true` 且上游为 SSE（`text/event-stream`）时边读边吐。
- **阻断**：上游 HTTP 400 且为 Guardrail 阻断 JSON 时，解析 `error.cai_error.scanner_results` 中 `outcome == "failed"` 的项，拼装中文说明文案，并以 OpenAI `chat.completion` / `chat.completion.chunk`（流式）返回，**HTTP 状态码为 200**，`finish_reason` 为 **`stop`**。
- **解析失败**：仍返回 **HTTP 200**，内容为 OpenAI 形态的助手文本，其中说明无法解析的原因（见环境变量说明中的文案约定）。
- **占位**：`GET /v1/models` 与 `GET /models` 返回最小模型列表；`GET /health` 健康检查。
- **Dify 审查接口**：`POST /dify/moderation`（另支持 `POST /moderation`），支持 `app.moderation.input` 与 `app.moderation.output` 两种扩展点；将指定字段发送到 F5 `scans` 接口并据 `result.outcome` 判定是否违规。

## 环境变量

| 变量 | 含义 | 默认 |
|------|------|------|
| `UPSTREAM_BASE_URL` | 上游完整 URL（含路径，例如 `https://.../chat/completions`） | （必填，未设置时对 completions 返回 500） |
| `HOST` | 监听地址 | `0.0.0.0` |
| `PORT` | 监听端口 | `8080` |
| `HTTP_TIMEOUT_CONNECT` | 连接超时（秒） | `30` |
| `HTTP_TIMEOUT_READ` | 读超时（秒） | `600` |
| `HTTP_TIMEOUT_WRITE` | 写超时（秒） | `60` |
| `HTTP_TIMEOUT_POOL` | 池超时（秒） | `5` |
| `MAX_CONNECTIONS` | httpx 最大连接数 | `200` |
| `MAX_KEEPALIVE_CONNECTIONS` | httpx 最大保持连接数 | `50` |
| `MODELS_LIST_ID` | `/v1/models` 中返回的模型 id | `placeholder-model` |
| `MODELS_LIST_OWNED_BY` | `/v1/models` 中 `owned_by` | `proxy` |
| `F5_SCANS_URL` | F5 scans 完整 URL（例如 `https://calypsoai.app/backend/v1/scans`） | 空 |
| `F5_SCANS_API_KEY` | F5 scans 的 Bearer Token | 空 |
| `DIFY_MODERATION_TOKEN` | Dify 调用审查接口时的 Bearer Token（必填） | 空（未配置将导致 moderation 接口返回 500） |
| `MODERATION_INPUT_BLOCK_MESSAGE` | input 扩展点命中违规时的 `preset_response` | `请求经F5 Guardrail检查存在违规。` |
| `MODERATION_OUTPUT_BLOCK_MESSAGE` | output 扩展点命中违规时的 `preset_response` | `响应经F5 Guardrail检查存在违规。` |

## 安装与运行

```bash
cd openai-f5api-converter
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export UPSTREAM_BASE_URL="https://example.com/openai/your-route/chat/completions"
uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}"
```

在本仓库根目录执行，使 `app` 包可被解析（当前工作目录应在包含 `app/` 的目录下）。

## 客户端示例

将原指向 Guardrail 的 Base URL 改为本服务地址，并保留原有 `Authorization` 与 JSON 体：

```bash
curl -sS "$PROXY_BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

仅转发会话最新用户消息（last 模式）：

```bash
curl -sS "$PROXY_BASE/last/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "model":"deepseek-v4-flash",
    "messages":[
      {"role":"system","content":"你是助手"},
      {"role":"user","content":"第一问"},
      {"role":"assistant","content":"第一答"},
      {"role":"user","content":"第二问（仅这一条会被转发到上游）"}
    ],
    "stream": true
  }'
```

## 行为说明

### 阻断文案拼装规则

对每条 `outcome == "failed"` 的 scanner：

1. 输出一行 `scanner_id`；
2. 若 `message` 非空，再输出一行该 `message`；若为空则不输出 message 行。

文首固定为：`请求已被F5 Guardrail由以下策略阻断：`

### /last 路由的输入约束

- 请求体必须是 JSON 对象，且包含 `messages` 数组。
- `messages` 中必须至少有一条 `role=user` 消息。
- 不满足上述条件时，返回 HTTP 400（`invalid_request_error`）。

### Dify moderation 接口

- 接口：`POST /dify/moderation`（或 `/moderation`）
- 鉴权：必须携带 `Authorization: Bearer $DIFY_MODERATION_TOKEN`；若服务端未配置 `DIFY_MODERATION_TOKEN`，接口返回 500。
- 支持扩展点：
  - `app.moderation.input`：读取 `params.query` 送审
  - `app.moderation.output`：读取 `params.text` 送审
- 送审请求：`POST $F5_SCANS_URL`，`Authorization: Bearer $F5_SCANS_API_KEY`，JSON 体为 `{"input":"<待审文本>"}`。
- 判定规则：
  - `result.outcome == "flagged"`：违规，返回 `action=direct_output`
  - `result.outcome == "redacted"`：返回 `action=overridden`，并将 scans 返回的 `redactedInput` 写入对应字段（input 写入 `query`，output 写入 `text`）

命中违规时返回（按扩展点区分，且可由环境变量配置）：

- `app.moderation.input`：`preset_response = $MODERATION_INPUT_BLOCK_MESSAGE`
- `app.moderation.output`：`preset_response = $MODERATION_OUTPUT_BLOCK_MESSAGE`

示例：

```json
{
  "flagged": true,
  "action": "direct_output",
  "preset_response": "请求经F5 Guardrail检查存在违规。"
}
```

未命中时返回：

```json
{
  "flagged": false,
  "action": "direct_output",
  "preset_response": ""
}
```

说明：
- 为避免漏检，若 scans 接口异常（超时、非 200、返回格式错误、缺少 `result.outcome`），当前实现采用**保守拦截**策略：同样返回 `flagged=true` + `direct_output`，并在响应里附带 `error` 字段便于排障。
- 为避免 Dify 在 input/output 连续审查时出现文案二次覆盖：当 `app.moderation.output` 收到的 `params.text` 恰好等于 `MODERATION_INPUT_BLOCK_MESSAGE`，服务会直接放行（`flagged=false`），不再改写成 output 文案。
- 若 scans 返回 `outcome=redacted` 但缺少 `redactedInput`，当前实现回退为保守拦截（`direct_output` + `error`）。

### 流式成功路径说明

当客户端 `stream: true` 且上游返回 `text/event-stream` 时，实现上会先发起一次探测请求，再发起一次完整流式转发，以保证连接生命周期与 SSE 透传正确（上游可能收到两次成功的流式请求）。若需在极高 QPS 下避免双倍请求，可后续改为会话级优化。

### 上游非 200 / 非 400

响应体与状态码尽量原样返回客户端（非阻断层）；客户端若要求严格 SSE，请按需在本服务外加网关或扩展映射逻辑。

## 许可证

按项目需要自行补充。
