# ChatGemini

<p align="center">
  <img src="logo.png" width="160" alt="ChatGemini logo">
</p>

<p align="center">
  <strong>稳定、纯聊天的 Gemini Web 网关，兼容 OpenAI 和 NewAPI。</strong><br>
  专门服务 NewAPI、OpenWebUI 和普通聊天机器人客户端。
</p>

<p align="center">
  <a href="https://github.com/ksda9001/ChatGemini/stargazers"><img src="https://img.shields.io/github/stars/ksda9001/ChatGemini?style=flat-square" alt="GitHub stars"></a>
  <a href="https://github.com/ksda9001/ChatGemini/network/members"><img src="https://img.shields.io/github/forks/ksda9001/ChatGemini?style=flat-square" alt="GitHub forks"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/ksda9001/ChatGemini?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker ready">
</p>

<p align="center"><a href="README.md">English</a></p>

> ChatGemini 是 Gemini 网页服务的非官方兼容层，不是 Google 官方 API。上游行为、账号权限和频率限制仍由 Google 决定。

## 为什么做 ChatGemini

ChatGemini 只做一件事：提供尽可能稳定的 Gemini 网页聊天。主接口是 OpenAI Chat Completions，同时为已有的 NewAPI Gemini 渠道提供一个精简的 Gemini 原生文本适配层。

项目彻底移除了编程 Agent 协议、工具提示、函数调用解析、Anthropic Messages、OpenAI Responses 和 Google 原生 Function Calling。协议面更小，意味着 prompt 冲突更少、请求更短，也更不容易把 NewAPI 或 OpenWebUI 的普通聊天误判成工具任务。

- OpenAI 兼容 `/v1/chat/completions`
- 为 NewAPI `/v1beta` 渠道提供 Gemini 原生纯文本兼容
- 普通与 SSE 流式聊天
- NewAPI、OpenWebUI 友好的流式响应
- Gemini 思考期间持续发送心跳
- 空上游回复自动重试
- `BardErrorInfo 1155` 输出上限自动续写
- 可选登录态 Gemini Web 会话
- 只保存普通聊天映射的 SQLite
- OpenWebUI 标题、标签和后续问题使用临时聊天
- OpenAI 多模态消息格式的可选图片输入
- Docker、Podman 和原生 Python 部署

## 明确不支持

ChatGemini 不提供：

- Tool Calling 或 Function Calling
- Codex Responses API
- Claude/Anthropic Messages API
- 编程 Agent 执行循环
- Google 原生工具或函数调用

如果客户端附带 OpenAI 的 `tools`、`functions`、`tool_choice`，或 Gemini 原生的 `tools`、`toolConfig`、`functionCall`、`functionResponse`，ChatGemini 都会忽略它们，只返回纯文本。工具 schema 和结果永远不会注入 Gemini prompt。

## API 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/` | 服务状态 |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/v1/models` | OpenAI 兼容模型列表 |
| `POST` | `/v1/chat/completions` | 普通和 SSE 流式聊天 |
| `GET` | `/v1beta/models` | 供 NewAPI 使用的 Gemini 兼容模型列表 |
| `POST` | `/v1beta/models/{model}:generateContent` | Gemini 兼容纯文本聊天 |
| `POST` | `/v1beta/models/{model}:streamGenerateContent` | Gemini 兼容纯文本 SSE |

`/v1beta` 适配层刻意只支持文本。它用于让 Gemini 格式的 NewAPI 渠道继续工作，不会恢复 Google Function Calling 或 Agent 行为。

## 快速开始

### Docker

```bash
git clone https://github.com/ksda9001/ChatGemini.git
cd ChatGemini

cp config.example.json config.json
docker build -t chatgemini .

docker run -d \
  --name chatgemini \
  --restart unless-stopped \
  -p 8081:8081 \
  -v "$PWD/config.json:/app/config.json:ro" \
  -v chatgemini-data:/app/data \
  chatgemini
```

Podman 可以使用相同参数。

### Docker Compose

```bash
cp config.example.json config.json
docker compose up -d --build
```

### Python

```bash
git clone https://github.com/ksda9001/ChatGemini.git
cd ChatGemini

python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
python -m chatgemini --config config.json
```

服务默认运行在 `http://127.0.0.1:8081`。

## 第一次聊天请求

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-3.5-flash",
    "messages": [{"role": "user", "content": "你好！"}]
  }'
```

流式请求：

```bash
curl -N http://127.0.0.1:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-3.5-flash",
    "messages": [{"role": "user", "content": "写一个短故事。"}],
    "stream": true,
    "stream_options": {"include_usage": true}
  }'
```

## 接入 NewAPI

推荐在 NewAPI 中创建 OpenAI 兼容渠道：

| 字段 | 值 |
| --- | --- |
| 渠道类型 | OpenAI |
| Base URL | `http://你的服务器:8081` |
| API Key | `api_keys` 为空时填任意占位值；否则填写配置中的密钥 |
| 模型 | `gemini-3.5-flash` |

如果你的 NewAPI 版本要求带版本路径，使用 `http://你的服务器:8081/v1`。

已有的 Gemini 格式 NewAPI 渠道也无需重建。将 Base URL 指向 `http://你的服务器:8081` 即可；ChatGemini 支持原生 `generateContent` 和 `streamGenerateContent` 纯文本路由。这个兼容模式会忽略 Gemini 工具/函数配置。Anthropic 渠道仍然不支持。

建议：

- 开启流式输出
- 渠道响应超时应大于反向代理超时
- 不要为这个渠道开启 Function Calling
- 只有客户端强制使用其他模型名时才配置模型映射

未知模型名会自动回退到 `default_model`，因此大多数模型映射错误不会直接中断聊天。

## 接入 OpenWebUI

添加 OpenAI 兼容连接：

```text
URL:     http://你的服务器:8081/v1
API key: api_keys 为空时填任意占位值
```

OpenWebUI 会读取 `/v1/models`，随后可进行普通或流式聊天。默认的标题、标签、后续问题和图片提示词后台请求会作为 Gemini 临时聊天发送，不会污染登录账号的可见历史。

为了得到最可预测的结果，请关闭 ChatGemini 模型的 OpenWebUI 工具/函数。即使误发工具定义，服务也会忽略，但没有必要浪费请求体积。

## OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8081/v1",
    api_key="placeholder",
)

response = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{"role": "user", "content": "简单解释相对论。"}],
)

print(response.choices[0].message.content)
```

## 模型

| 模型 | 说明 |
| --- | --- |
| `gemini-3.5-flash` | 快速通用聊天 |
| `gemini-3.5-flash-thinking` | 更深推理和更长输出 |
| `gemini-3.5-flash-thinking-lite` | 自适应思考模式 |
| `gemini-3.1-pro` | Pro 偏好；真正路由需要符合条件的账号 |
| `gemini-3.1-pro-enhanced` | 实验性增强 Pro 模式 |
| `gemini-auto` | 上游自动选择 |
| `gemini-flash-lite` | 轻量快速模式 |

使用模型后缀调整思考深度：

```text
gemini-3.5-flash-thinking@think=0
gemini-3.5-flash-thinking@think=2
gemini-3.5-flash-thinking@think=4
```

## 匿名与登录模式

ChatGemini 默认可以匿名使用 Gemini Web，这是最简单的方式。OpenWebUI 和 NewAPI 每次请求都会发送完整消息历史，因此普通多轮聊天仍然有效。

登录后的浏览器会话可以增加：

- 账号可用的模型路由
- Gemini Web conversation metadata
- 可选上游对话复用
- 后台 Cookie 刷新

Cookie 文件支持普通 `name=value` 字符串、紧凑 JSON、Chrome Cookie 数组和 Playwright 导出。至少保留同一浏览器会话中的 `__Secure-1PSID` 和 `__Secure-1PSIDTS`。

Cookie 是账号凭据。不要提交到 Git，不要发到公开 issue，也不要写进容器镜像。

登录模式配置：

```json
{
  "api_keys": ["change-this-private-key"],
  "cookie_file": "/app/cookie.json",
  "conversation_store_path": "/app/data/conversations.db",
  "reuse_upstream_sessions": true
}
```

只读挂载 Cookie：

```bash
docker run -d \
  --name chatgemini \
  --restart unless-stopped \
  -p 8081:8081 \
  -v "$PWD/config.json:/app/config.json:ro" \
  -v "$PWD/cookie.json:/app/cookie.json:ro" \
  -v chatgemini-data:/app/data \
  chatgemini
```

如果 Gemini 地址包含 `/u/1/`，将 `auth_user` 设置为 `"1"`。某些会话还需要把页面中的 `SNlM0e` 填入 `xsrf_token`。

## 纯聊天会话复用

启用 `reuse_upstream_sessions` 后，ChatGemini 会保存已完成聊天的 Gemini Web metadata。下一次 OpenAI 请求到来时，服务查找完全一致的上一段消息历史，只把新增用户消息发送到同一个 Gemini Web 对话。

SQLite 只包含普通对话映射：

```text
conversation_sessions
```

没有 Responses 对象、工具调用、call ID 或 Agent 状态表。如果 SQLite 不可用，ChatGemini 会记录缓存错误，然后改发压缩后的完整聊天历史，不会让缓存故障直接中断聊天。

## 稳定性设计

- OpenAI SSE role、content、stop、可选 usage 和 `[DONE]` 帧
- 为 Nginx 设置 `X-Accel-Buffering: no`
- 等待 Gemini 时发送 SSE 注释心跳
- 限制请求体和聊天历史大小
- 确定性删除旧消息
- 空响应自动重试
- `BardErrorInfo 1155` 自动续写
- 登录网页流在首字前失败时回退 direct transport
- 按浏览器规则过滤过期 Cookie
- OpenWebUI 后台请求不进入账号可见历史

## 重要配置

从 [`config.example.json`](config.example.json) 开始。

| 配置 | 用途 | 默认行为 |
| --- | --- | --- |
| `api_keys` | 保护 `/v1/*` | 空数组关闭认证 |
| `cookie_file` | 浏览器会话文件 | `null` 使用匿名模式 |
| `proxy` | 访问 Google 的 HTTP 代理 | `null` 使用直连/系统网络 |
| `conversation_store_path` | 纯聊天 SQLite | Docker 中为 `/app/data/conversations.db` |
| `reuse_upstream_sessions` | 复用 Gemini Web metadata | 默认关闭 |
| `max_history_messages` | 最近聊天消息上限 | `60` |
| `max_history_chars` | prompt 字符近似上限 | `80000` |
| `sse_heartbeat_sec` | 心跳间隔 | `10` 秒 |
| `continuation_attempts` | 输出上限续写次数 | `2` |

如果服务暴露在 localhost 或可信网络之外，请先设置私有 API Key。

## 故障排查

### NewAPI 或 OpenWebUI 报超时

确认请求路径上的每一层反向代理都允许流式输出并关闭 buffering。ChatGemini 每隔 `sse_heartbeat_sec` 发送心跳，但代理 read timeout 仍需大于这个间隔。

### 回复为空或被截断

1. 检查 `curl http://127.0.0.1:8081/healthz`。
2. 查看容器日志。
3. 确认服务器能访问 `https://gemini.google.com`。
4. 无法直连时配置 `proxy`。
5. 更新失效 Cookie，或临时关闭 `reuse_upstream_sessions`。

### 工具不能调用

这是预期行为。ChatGemini 是纯聊天服务，请把它作为 OpenAI Chat 渠道，并在前端关闭该模型的工具/函数。

### 登录对话不能续接

开启 `reuse_upstream_sessions`，持久化 `/app/data`，并确认 Cookie 账号处于可用登录状态。即使 metadata 续接失败，聊天仍会回退到完整历史方式。

## 安全与限制

- Gemini Web 是非官方上游，可能随时改变。
- 浏览器 Cookie 是敏感凭据，Google 可能使其失效。
- 真正 Pro 权限取决于账号资格，不是有 Cookie 就能升级。
- 频率限制和风控仍然有效。
- 图片上传依赖登录态 Gemini Web 上传路径，稳定性可能低于文本。
- 如果业务需要合同级稳定性，应使用 Google 官方 API。

## 开发

```bash
python -m unittest discover -s tests -q
python -m py_compile chatgemini/*.py tests/test_chatgemini.py
git diff --check
```

测试覆盖 OpenAI 和 Gemini 兼容文本响应格式、流式输出、usage 帧、心跳、路由移除、工具 schema 忽略、SQLite 续接、Cookie 过期和自动续写。

## 致谢

- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API)：动态 Gemini Web 会话客户端
- 开源 Gemini Web 兼容项目社区

## License

[MIT](LICENSE)

如果 ChatGemini 让你的 NewAPI 或 OpenWebUI 部署更简单，欢迎点一个 star，让更多聊天机器人开发者找到它。
