"""A tiny client for any OpenAI-compatible chat API, defaulting to OpenRouter.

Standard library only. Reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from the environment
(with optional local .env fallback when environment variables are unset).
Every call appends its token counts and endpoint to usage.jsonl.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = ""
DEFAULT_MAX_ATTEMPTS = 2  # 1 initial call + at most 1 retry on transient errors

_usage_lock = threading.Lock()
_no_json_lock = threading.Lock()
_NO_JSON_MODE_MODELS = set()


def _load_env_fallback():
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k not in os.environ:
                    os.environ[k] = v


# Automatically load on import
_load_env_fallback()


def _json_mode_disabled():
    val = (os.environ.get("LLM_JSON_MODE") or "").strip().lower()
    return val in ("0", "false", "off", "no")


def _is_hard_quota_error(code, err_body):
    if code != 429:
        return False
    lower = err_body.lower()
    hard_markers = ("per-day", "daily", "quota", "credit", "insufficient", "limit reached")
    return any(m in lower for m in hard_markers)


def _should_fallback_without_json_mode(code, err_body, body):
    if code != 400 or "response_format" not in body:
        return False
    lower = err_body.lower()
    non_format_markers = (
        "valid model",
        "unknown model",
        "model not found",
        "context length",
        "maximum context",
        "insufficient credits",
    )
    return not any(m in lower for m in non_format_markers)


def _extract_message_content(msg):
    """Extract text content from an OpenAI/OpenRouter choice message."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _build_request(endpoint_url, api_key, body):
    return urllib.request.Request(
        endpoint_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": "Xpert-ColdStart/1.0",
        },
    )


def chat(messages, model=None, temperature=0, max_tokens=None, response_format=None, timeout=300):
    _load_env_fallback()
    api_key = (os.environ.get("LLM_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "LLM_API_KEY environment variable is not set. "
            "Please set it before calling chat()."
        )

    base = (os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    endpoint_url = base + "/chat/completions"
    selected_model = (model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL or "").strip()
    if not selected_model:
        raise ValueError(
            "LLM_MODEL environment variable is not set. "
            "Please set LLM_MODEL or pass model= explicitly before calling chat()."
        )

    body = {
        "model": selected_model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

    with _no_json_lock:
        json_mode_known_unsupported = (endpoint_url, selected_model) in _NO_JSON_MODE_MODELS
    if response_format and not _json_mode_disabled() and not json_mode_known_unsupported:
        body["response_format"] = response_format

    try:
        max_attempts = max(1, int(os.environ.get("LLM_MAX_RETRIES") or DEFAULT_MAX_ATTEMPTS))
    except ValueError:
        max_attempts = DEFAULT_MAX_ATTEMPTS

    started = time.time()
    data = None
    retried_without_json = False
    attempt = 0

    while attempt < max_attempts:
        req = _build_request(endpoint_url, api_key, body)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if retried_without_json:
                with _no_json_lock:
                    _NO_JSON_MODE_MODELS.add((endpoint_url, selected_model))
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if not retried_without_json and _should_fallback_without_json_mode(e.code, err_body, body):
                body.pop("response_format", None)
                retried_without_json = True
                continue
            if e.code in (429, 502, 503, 504) and not _is_hard_quota_error(e.code, err_body) and attempt < max_attempts - 1:
                retry_after = (e.headers.get("Retry-After") if e.headers else None) or ""
                try:
                    wait_time = min(15.0, max(1.0, float(retry_after)))
                except ValueError:
                    wait_time = min(15, (2 ** attempt) * 3)
                attempt += 1
                time.sleep(wait_time)
                continue
            raise RuntimeError(f"LLM API Error (HTTP {e.code}) at {endpoint_url}: {err_body}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected non-object API response from {endpoint_url}: {type(data).__name__}")
    if data.get("error"):
        raise RuntimeError(f"LLM API returned error payload at {endpoint_url}: {data['error']}")

    usage = data.get("usage") or {}
    usage_path = Path(__file__).resolve().parent / "usage.jsonl"
    usage_record = {
        "model": body["model"],
        "endpoint": endpoint_url,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "seconds": round(time.time() - started, 2),
    }
    if data.get("model") and data.get("model") != body["model"]:
        usage_record["response_model"] = data.get("model")

    with _usage_lock:
        with open(usage_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(usage_record) + "\n")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"LLM API response missing 'choices' at {endpoint_url}: keys={sorted(data.keys())}")

    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = choice.get("finish_reason")
    msg = choice.get("message")
    if not isinstance(msg, dict):
        raise RuntimeError(f"LLM API response missing 'message' object (finish_reason={finish_reason!r}).")

    content = _extract_message_content(msg)
    if not content or not content.strip():
        has_reasoning = bool(msg.get("reasoning") or msg.get("reasoning_content") or msg.get("reasoning_details"))
        refusal = msg.get("refusal")
        raise RuntimeError(
            f"Model '{body['model']}' returned empty content "
            f"(finish_reason={finish_reason!r}, completion_tokens={usage.get('completion_tokens')}, "
            f"has_reasoning={has_reasoning}, refusal={refusal!r})."
        )

    return content
