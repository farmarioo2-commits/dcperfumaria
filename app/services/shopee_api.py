from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import httpx

BASE_URL = os.getenv("SHOPEE_API_BASE_URL", "https://partner.shopeemobile.com")


def configured() -> bool:
    return bool(
        os.getenv("SHOPEE_PARTNER_ID", "").strip()
        and os.getenv("SHOPEE_PARTNER_KEY", "").strip()
        and os.getenv("SHOPEE_REDIRECT_URL", "").strip()
    )


def _partner_id() -> int:
    value = os.getenv("SHOPEE_PARTNER_ID", "").strip()
    if not value:
        raise ValueError("SHOPEE_PARTNER_ID não configurado")
    return int(value)


def _partner_key() -> str:
    value = os.getenv("SHOPEE_PARTNER_KEY", "").strip()
    if not value:
        raise ValueError("SHOPEE_PARTNER_KEY não configurado")
    return value


def _sign(base_string: str) -> str:
    return hmac.new(
        _partner_key().encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def authorization_url(state: str) -> str:
    partner_id = _partner_id()
    redirect = os.getenv("SHOPEE_REDIRECT_URL", "").strip()
    if not redirect:
        raise ValueError("SHOPEE_REDIRECT_URL não configurado")
    path = "/api/v2/shop/auth_partner"
    timestamp = int(time.time())
    sign = _sign(f"{partner_id}{path}{timestamp}")
    params = {
        "partner_id": partner_id,
        "timestamp": timestamp,
        "sign": sign,
        "redirect": redirect,
        "state": state,
    }
    return f"{BASE_URL}{path}?{urlencode(params)}"


def exchange_code(code: str, shop_id: int) -> dict:
    partner_id = _partner_id()
    path = "/api/v2/auth/token/get"
    timestamp = int(time.time())
    sign = _sign(f"{partner_id}{path}{timestamp}")
    params = {
        "partner_id": partner_id,
        "timestamp": timestamp,
        "sign": sign,
    }
    payload = {
        "code": code,
        "shop_id": shop_id,
        "partner_id": partner_id,
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{BASE_URL}{path}",
            params=params,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if data.get("error"):
        raise ValueError(
            f"Shopee: {data.get('error')} - {data.get('message', '')}"
        )
    return data


def refresh_token(refresh_token_value: str, shop_id: int) -> dict:
    partner_id = _partner_id()
    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    sign = _sign(f"{partner_id}{path}{timestamp}")
    params = {
        "partner_id": partner_id,
        "timestamp": timestamp,
        "sign": sign,
    }
    payload = {
        "refresh_token": refresh_token_value,
        "shop_id": shop_id,
        "partner_id": partner_id,
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{BASE_URL}{path}",
            params=params,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if data.get("error"):
        raise ValueError(
            f"Shopee: {data.get('error')} - {data.get('message', '')}"
        )
    return data


def shop_get(path: str, access_token: str, shop_id: int, **params) -> dict:
    partner_id = _partner_id()
    timestamp = int(time.time())
    base_string = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    sign = _sign(base_string)
    query = {
        "partner_id": partner_id,
        "timestamp": timestamp,
        "access_token": access_token,
        "shop_id": shop_id,
        "sign": sign,
        **params,
    }
    with httpx.Client(timeout=90.0) as client:
        response = client.get(f"{BASE_URL}{path}", params=query)
        response.raise_for_status()
        data = response.json()
    if data.get("error"):
        raise ValueError(
            f"Shopee: {data.get('error')} - {data.get('message', '')}"
        )
    return data
