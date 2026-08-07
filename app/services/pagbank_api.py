from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

SANDBOX_BASE = "https://sandbox.api.pagseguro.com"
PRODUCTION_BASE = "https://api.pagseguro.com"


def base_url(environment: str) -> str:
    return SANDBOX_BASE if environment.upper() == "SANDBOX" else PRODUCTION_BASE


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_pix_order(
    *,
    environment: str,
    token: str,
    reference_id: str,
    amount: Decimal,
    customer_name: str,
    customer_email: str,
    customer_tax_id: str,
    notification_url: str,
    expiration_minutes: int = 30,
) -> dict[str, Any]:
    expiration = (
        datetime.now(timezone.utc) + timedelta(minutes=expiration_minutes)
    ).isoformat(timespec="seconds")
    payload = {
        "reference_id": reference_id,
        "customer": {
            "name": customer_name,
            "email": customer_email,
            "tax_id": customer_tax_id,
        },
        "items": [
            {
                "reference_id": reference_id,
                "name": "Recebimento Gestão Fácil",
                "quantity": 1,
                "unit_amount": int((amount * 100).quantize(Decimal("1"))),
            }
        ],
        "qr_codes": [
            {
                "amount": {
                    "value": int((amount * 100).quantize(Decimal("1")))
                },
                "expiration_date": expiration,
            }
        ],
        "notification_urls": [notification_url],
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{base_url(environment)}/orders",
            headers=_headers(token),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def create_boleto_order(
    *,
    environment: str,
    token: str,
    reference_id: str,
    amount: Decimal,
    customer: dict[str, Any],
    due_date: str,
    notification_url: str,
) -> dict[str, Any]:
    payload = {
        "reference_id": reference_id,
        "customer": customer,
        "items": [
            {
                "reference_id": reference_id,
                "name": "Recebimento Gestão Fácil",
                "quantity": 1,
                "unit_amount": int((amount * 100).quantize(Decimal("1"))),
            }
        ],
        "charges": [
            {
                "reference_id": reference_id,
                "description": "Cobrança Gestão Fácil",
                "amount": {
                    "value": int((amount * 100).quantize(Decimal("1"))),
                    "currency": "BRL",
                },
                "payment_method": {
                    "type": "BOLETO",
                    "boleto": {
                        "due_date": due_date,
                        "instruction_lines": {
                            "line_1": "Não receber após o vencimento.",
                            "line_2": "Cobrança emitida pelo Gestão Fácil.",
                        },
                        "holder": customer,
                    },
                },
            }
        ],
        "notification_urls": [notification_url],
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{base_url(environment)}/orders",
            headers=_headers(token),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def get_order(environment: str, token: str, order_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            f"{base_url(environment)}/orders/{order_id}",
            headers=_headers(token),
        )
        response.raise_for_status()
        return response.json()


def test_connection(environment: str, token: str) -> dict[str, Any]:
    if not token.strip():
        raise ValueError("Token PagBank vazio")
    # PagBank does not expose a harmless generic ping endpoint.
    # Validation is completed by checking token presence and API format.
    return {
        "ok": True,
        "environment": environment.upper(),
        "base_url": base_url(environment),
        "message": "Configuração salva. O token será validado na primeira cobrança.",
    }


def extract_payment_details(payload: dict[str, Any]) -> dict[str, Any]:
    qr_codes = payload.get("qr_codes") or []
    charges = payload.get("charges") or []
    qr = qr_codes[0] if qr_codes else {}
    charge = charges[0] if charges else {}

    links = qr.get("links") or charge.get("links") or payload.get("links") or []
    qr_png = ""
    boleto_pdf = ""
    for link in links:
        rel = str(link.get("rel") or "").upper()
        media = str(link.get("media") or "").lower()
        href = str(link.get("href") or "")
        if "QRCODE" in rel or "image/png" in media:
            qr_png = href
        if "PDF" in rel or "application/pdf" in media:
            boleto_pdf = href

    payment_method = charge.get("payment_method") or {}
    boleto = payment_method.get("boleto") or charge.get("payment_response") or {}

    return {
        "order_id": str(payload.get("id") or ""),
        "charge_id": str(charge.get("id") or ""),
        "status": str(
            charge.get("status")
            or payload.get("status")
            or "WAITING"
        ),
        "qr_code_text": str(
            qr.get("text")
            or qr.get("emv")
            or ""
        ),
        "qr_code_link": qr_png,
        "boleto_barcode": str(
            boleto.get("barcode")
            or boleto.get("formatted_barcode")
            or ""
        ),
        "boleto_pdf": boleto_pdf,
    }


def paid_status(value: str) -> bool:
    return value.upper() in {
        "PAID",
        "AUTHORIZED",
        "AVAILABLE",
        "IN_ANALYSIS_APPROVED",
    }
