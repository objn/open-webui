import json
import sys
from datetime import datetime, timezone

from open_webui.env import DEBUG_CHAT

SEPARATOR = "-" * 60


def _safe_dump(data, max_length: int = 4000) -> str:
    try:
        if isinstance(data, (dict, list)):
            text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
        elif isinstance(data, str):
            text = data
        else:
            text = str(data)
    except Exception:
        text = repr(data)

    if len(text) > max_length:
        text = text[:max_length] + f"\n... (truncated, total {len(text)} chars)"
    return text


def log_chat(method_name: str, direction: str, data=None):
    """
    Prints a debug chat log when DEBUG_CHAT is enabled.
    Uses print() directly to bypass loguru/logging config and ensure
    output always appears in Docker logs / stdout.
    """
    if not DEBUG_CHAT:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    body = _safe_dump(data) if data is not None else "(empty)"

    print(
        f"\n{SEPARATOR}\n"
        f"[{now}] : {method_name}\n"
        f"  [{direction.upper()}]\n"
        f"{body}\n"
        f"{SEPARATOR}",
        flush=True,
    )
