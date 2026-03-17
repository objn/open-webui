# DEBUG_CHAT — Chat Request/Response Logging

## Overview

When `DEBUG_CHAT=true` is set, Open WebUI logs every chat request and response flowing through the system to stdout. This is useful for debugging, inspecting payloads, and tracing the full lifecycle of a chat completion call.

**Default**: `false` (no overhead when disabled)

---

## Setup

Add to your `.env` file:

```env
DEBUG_CHAT=true
```

Or set as an environment variable:

```bash
export DEBUG_CHAT=true
```

Restart the backend for the change to take effect.

---

## Log Format

```
------------------------------------------------------------
[2026-03-09 14:30:12.345] : <method_name>
  [REQUEST or RESPONSE]
{
  "model": "gpt-4",
  "messages": [...]
}
------------------------------------------------------------
```

Each log entry includes:
- **Datetime** (UTC, millisecond precision)
- **Method name** — which function in the pipeline is logging
- **Direction** — `REQUEST` (incoming) or `RESPONSE` (outgoing)
- **Body** — the JSON payload (truncated at 4,000 chars for safety)

---

## Hook Points

The logging is placed at 7 key points across the chat completion flow:

| # | File | Method | What it logs |
|---|------|--------|-------------|
| 1 | `main.py` | `chat_completion` | Incoming request from the frontend |
| 2 | `utils/chat.py` | `generate_chat_completion` | Dispatcher entry (after metadata merge) |
| 3 | `utils/chat.py` | `generate_ollama_chat_completion` / `generate_openai_chat_completion` / `generate_function_chat_completion` | Request routed to specific backend |
| 4 | `utils/middleware.py` | `process_chat_payload` | Request after full payload processing (filters, RAG, tools, files) |
| 5 | `utils/middleware.py` | `process_chat_response` | Response metadata (streaming vs non-streaming, content type, model) |
| 6 | `routers/openai.py` | `openai.generate_chat_completion` | Raw HTTP request to OpenAI API + response |
| 7 | `routers/ollama.py` | `ollama.send_post_request` | Raw HTTP request to Ollama API + response |

---

## Flow Diagram

```
Frontend
  │
  ▼
[1] main.py :: chat_completion ─────────────── REQUEST (raw from frontend)
  │
  ▼
[4] middleware.py :: process_chat_payload ───── REQUEST (after filters, RAG, tools)
  │
  ▼
[2] utils/chat.py :: generate_chat_completion ─ REQUEST (dispatcher entry)
  │
  ├─► [3] Ollama path
  │     └─► [7] ollama.send_post_request ───── REQUEST → Ollama API
  │                                             RESPONSE ← Ollama API
  │
  ├─► [3] OpenAI path
  │     └─► [6] openai.generate_chat_completion REQUEST → OpenAI API
  │                                               RESPONSE ← OpenAI API
  │
  └─► [3] Pipeline/Function path ──────────── REQUEST → Function pipeline
  │
  ▼
[5] middleware.py :: process_chat_response ──── RESPONSE (back to frontend)
  │
  ▼
Frontend
```

---

## Files Modified

| File | Change |
|------|--------|
| `backend/open_webui/env.py` | Added `DEBUG_CHAT` env var (default `false`) |
| `backend/open_webui/utils/debug_chat.py` | **New file** — `log_chat()` utility function |
| `backend/open_webui/main.py` | Added `log_chat` call at `chat_completion` entry |
| `backend/open_webui/utils/chat.py` | Added `log_chat` calls at dispatcher + routing points |
| `backend/open_webui/utils/middleware.py` | Added `log_chat` calls at `process_chat_payload` and `process_chat_response` |
| `backend/open_webui/routers/openai.py` | Added `log_chat` calls around HTTP request/response to OpenAI |
| `backend/open_webui/routers/ollama.py` | Added `log_chat` calls around HTTP request/response to Ollama |

---

## Example Output

When sending "Hello!" to GPT-4 with `DEBUG_CHAT=true`:

```
------------------------------------------------------------
[2026-03-09 14:30:12.100] : chat_completion
  [REQUEST]
{
  "model": "gpt-4",
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true
}
------------------------------------------------------------

------------------------------------------------------------
[2026-03-09 14:30:12.105] : process_chat_payload
  [REQUEST]
{
  "model": "gpt-4",
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true
}
------------------------------------------------------------

------------------------------------------------------------
[2026-03-09 14:30:12.200] : generate_chat_completion
  [REQUEST]
{
  "model": "gpt-4",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true
}
------------------------------------------------------------

------------------------------------------------------------
[2026-03-09 14:30:12.205] : generate_openai_chat_completion
  [REQUEST]
{
  "model": "gpt-4",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true
}
------------------------------------------------------------

------------------------------------------------------------
[2026-03-09 14:30:12.210] : openai.generate_chat_completion
  [REQUEST]
{
  "url": "https://api.openai.com/v1/chat/completions",
  "payload": {
    "model": "gpt-4",
    "messages": [...],
    "stream": true
  }
}
------------------------------------------------------------

------------------------------------------------------------
[2026-03-09 14:30:12.850] : openai.generate_chat_completion
  [RESPONSE]
{
  "status": 200,
  "streaming": true
}
------------------------------------------------------------

------------------------------------------------------------
[2026-03-09 14:30:12.855] : process_chat_response
  [RESPONSE]
{
  "streaming": true,
  "content_type": "text/event-stream",
  "model": "gpt-4"
}
------------------------------------------------------------
```

---

## Notes

- **Zero overhead when disabled** — `log_chat()` returns immediately if `DEBUG_CHAT=false`
- **Payload truncation** — bodies larger than 4,000 characters are truncated to prevent log flooding
- **Streaming responses** — only metadata is logged (status, streaming flag), not the full stream content
- **UTC timestamps** — all timestamps use UTC with millisecond precision
