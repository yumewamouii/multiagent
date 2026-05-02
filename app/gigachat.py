import os
import uuid
import requests
import time


import urllib3
urllib3.disable_warnings()

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1"


_access_token: str | None = None
_token_expire: float = 0


def get_access_token() -> str:
    global _access_token, _token_expire
    if _access_token and time.time() < _token_expire:
        return _access_token
    auth_key = os.getenv("GIGACHAT_API_KEY")
    if not auth_key:
        raise RuntimeError("GIGACHAT_API_KEY not set")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {auth_key}",
    }
    data = {
        "scope": "GIGACHAT_API_PERS"
    }
    r = requests.post(OAUTH_URL, headers=headers, data=data, verify=False)
    r.raise_for_status()
    result = r.json()
    _access_token = result["access_token"]
    expires_at = result.get("expires_at")
    if expires_at:
        _token_expire = expires_at / 1000  # у GigaChat ms
    else:
        _token_expire = time.time() + 1700  # fallback
    return _access_token


def gigachat_request(path: str, payload: dict | None = None, method: str = "POST") -> dict:
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{API_URL}/{path}"
    if method == "GET":
        r = requests.get(url, headers=headers, verify=False)
    else:
        r = requests.post(url, headers=headers, json=payload, verify=False)

    r.raise_for_status()
    return r.json()