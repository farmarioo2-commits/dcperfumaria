from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx


SUPPORTED_PROVIDERS = {
    "BANCO_DO_BRASIL": {
        "label": "Banco do Brasil",
        "status": "CREDENCIAIS_NECESSARIAS",
        "fields": ["client_id", "client_secret", "developer_application_key"],
    },
    "ITAU": {
        "label": "Itaú",
        "status": "CREDENCIAIS_NECESSARIAS",
        "fields": ["client_id", "client_secret", "certificate", "private_key"],
    },
    "BRADESCO": {
        "label": "Bradesco",
        "status": "CREDENCIAIS_NECESSARIAS",
        "fields": ["client_id", "client_secret", "certificate", "private_key"],
    },
    "SANTANDER": {
        "label": "Santander",
        "status": "CREDENCIAIS_NECESSARIAS",
        "fields": ["client_id", "client_secret", "workspace_id"],
    },
    "SICREDI": {
        "label": "Sicredi",
        "status": "CREDENCIAIS_NECESSARIAS",
        "fields": ["client_id", "client_secret", "cooperative", "account"],
    },
    "SICOOB": {
        "label": "Sicoob",
        "status": "CREDENCIAIS_NECESSARIAS",
        "fields": ["client_id", "client_secret", "cooperative", "account"],
    },
    "GENERIC_JSON": {
        "label": "Provedor genérico JSON",
        "status": "DISPONIVEL",
        "fields": ["base_url", "token", "boletos_path"],
    },
}


def only_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def provider_catalog() -> list[dict[str, Any]]:
    return [
        {"code": code, **config}
        for code, config in SUPPORTED_PROVIDERS.items()
    ]


def validate_credentials(provider: str, credentials: dict[str, Any]) -> None:
    config = SUPPORTED_PROVIDERS.get(provider)
    if not config:
        raise ValueError("Provedor DDA não suportado")
    missing = [
        field for field in config["fields"]
        if not str(credentials.get(field, "")).strip()
    ]
    if missing:
        raise ValueError(
            "Preencha as credenciais: " + ", ".join(missing)
        )


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern).date()
        except ValueError:
            continue
    return None


def normalize_boleto(item: dict[str, Any], provider: str) -> dict[str, Any]:
    due_date = _parse_date(
        item.get("due_date")
        or item.get("data_vencimento")
        or item.get("vencimento")
    )
    if due_date is None:
        raise ValueError("Boleto sem data de vencimento")

    amount_raw = (
        item.get("amount")
        or item.get("value")
        or item.get("valor")
        or item.get("valor_original")
        or 0
    )
    amount = Decimal(str(amount_raw).replace(",", "."))

    external_id = str(
        item.get("id")
        or item.get("external_id")
        or item.get("nosso_numero")
        or item.get("seu_numero")
        or ""
    )
    line = only_digits(
        item.get("digitable_line")
        or item.get("linha_digitavel")
        or item.get("barcode")
        or item.get("codigo_barras")
    )
    if not external_id and not line:
        external_id = (
            f"{only_digits(item.get('beneficiary_document') or item.get('cnpj_beneficiario'))}"
            f"-{due_date.isoformat()}-{amount}"
        )

    return {
        "external_id": external_id,
        "digitable_line": line,
        "beneficiary_name": str(
            item.get("beneficiary_name")
            or item.get("beneficiario")
            or item.get("nome_beneficiario")
            or ""
        ),
        "beneficiary_document": only_digits(
            item.get("beneficiary_document")
            or item.get("cnpj_beneficiario")
            or item.get("cpf_cnpj_beneficiario")
        ),
        "payer_document": only_digits(
            item.get("payer_document")
            or item.get("cnpj_pagador")
            or item.get("cpf_cnpj_pagador")
        ),
        "issue_date": _parse_date(
            item.get("issue_date") or item.get("data_emissao")
        ),
        "due_date": due_date,
        "amount": amount,
        "bank_status": str(
            item.get("status") or item.get("situacao") or "EM_ABERTO"
        ),
        "provider": provider,
        "raw": item,
    }


def fetch_boletos(provider: str, credentials: dict[str, Any]) -> list[dict[str, Any]]:
    validate_credentials(provider, credentials)

    if provider != "GENERIC_JSON":
        raise RuntimeError(
            f"O conector {SUPPORTED_PROVIDERS[provider]['label']} está preparado, "
            "mas precisa ser finalizado com o contrato, ambiente e documentação "
            "liberados pelo banco para a empresa."
        )

    base_url = str(credentials["base_url"]).rstrip("/")
    path = str(credentials.get("boletos_path") or "/dda/boletos")
    token = str(credentials["token"])
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        response = client.get(f"{base_url}{path}", headers=headers)
        response.raise_for_status()
        payload = response.json()

    if isinstance(payload, dict):
        rows = (
            payload.get("boletos")
            or payload.get("data")
            or payload.get("items")
            or []
        )
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("Resposta do provedor DDA não contém uma lista de boletos")
    return [normalize_boleto(dict(item), provider) for item in rows]


def analyze_boleto(
    *,
    beneficiary_document: str,
    beneficiary_name: str,
    amount: Decimal,
    due_date: date,
    supplier_found: bool,
    invoice_found: bool,
    duplicated: bool,
    overdue: bool,
) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []

    if duplicated:
        return {
            "score": 0,
            "risk_level": "ALTO",
            "recommendation": "Bloquear: boleto duplicado.",
            "reasons": ["Linha digitável ou identificador já cadastrado."],
        }

    if supplier_found:
        score += 35
        reasons.append("Fornecedor reconhecido pelo CNPJ.")
    else:
        reasons.append("Fornecedor ainda não cadastrado.")

    if invoice_found:
        score += 45
        reasons.append("NF-e compatível localizada.")
    else:
        reasons.append("Nenhuma NF-e compatível foi localizada.")

    if beneficiary_document:
        score += 10
    if amount > 0:
        score += 10
    if overdue:
        reasons.append("Boleto já está vencido.")

    if score >= 80:
        risk = "BAIXO"
        recommendation = "Conferência automática aprovada; liberar para validação final."
    elif score >= 50:
        risk = "MEDIO"
        recommendation = "Revisar fornecedor, valor e nota antes de aprovar."
    else:
        risk = "ALTO"
        recommendation = "Não aprovar automaticamente; verificar possível cobrança indevida."

    return {
        "score": min(score, 100),
        "risk_level": risk,
        "recommendation": recommendation,
        "reasons": reasons,
    }
