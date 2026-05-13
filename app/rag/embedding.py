import logging
import os

import requests

log = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-qwen3-embedding-4b"
DEFAULT_VECTOR_SIZE = 384


def _fit_vector_size(vector: list[float], target_size: int) -> list[float]:
    if len(vector) == target_size:
        return vector
    if len(vector) > target_size:
        log.warning(
            "embedding length %s exceeds target size %s; truncating vector",
            len(vector),
            target_size,
        )
        return vector[:target_size]
    return vector + [0.0] * (target_size - len(vector))


def create_embedding(text: str) -> list[float] | None:
    if not text:
        return None

    try:
        base_url = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
        api_key = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
        model = os.getenv("LMSTUDIO_EMBEDDING_MODEL", "text-embedding-bge-small-en-v1.5")
        timeout_sec = float(os.getenv("LMSTUDIO_TIMEOUT_SEC", "60"))
        target_size = int(os.getenv("EMBEDDING_VECTOR_SIZE", str(DEFAULT_VECTOR_SIZE)))

        response = requests.post(
            f"{base_url}/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
                "input": text,
            },
            timeout=timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or []
        if not data:
            log.warning("embedding response has no data")
            return None
        vector = data[0].get("embedding")
        if not isinstance(vector, list):
            log.warning("embedding response has invalid vector format")
            return None
        normalized = [float(item) for item in vector]
        return _fit_vector_size(normalized, target_size)

    except Exception as e:
        log.warning(f"embedding error: {e}")
        return None