import json
import os
import re
from typing import Any

import requests

def _extract_text_from_openai(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def _lmstudio_chat(
    *,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 600,
) -> str:
    base_url = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
    model = os.getenv("LMSTUDIO_MODEL", "google/gemma-4-e4b")
    timeout_sec = float(os.getenv("LMSTUDIO_TIMEOUT_SEC", "60"))
    api_key = os.getenv("LMSTUDIO_API_KEY", "lm-studio")

    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=timeout_sec,
    )
    response.raise_for_status()
    return _extract_text_from_openai(response.json())


def chat_completion(
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 600,
) -> str:
    provider = os.getenv("LLM_PROVIDER", "lmstudio").strip().lower()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if provider == "lmstudio":
        return _lmstudio_chat(messages=messages, temperature=temperature, max_tokens=max_tokens)

    raise RuntimeError(f"unsupported_llm_provider: {provider}")


def parse_json_response(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
        t = t.strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(t[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = t[start : i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    break
    end = t.rfind("}")
    if end > start:
        try:
            return json.loads(t[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None
