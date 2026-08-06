from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID, ObjectIdentifier

CNPJ_OTHER_NAME_OID = ObjectIdentifier("2.16.76.1.3.3")
CNPJWS_URL = "https://publica.cnpj.ws/cnpj/{cnpj}"
BRASILAPI_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"


@dataclass(frozen=True)
class CertificateCompany:
    cnpj: str
    legal_name: str
    subject: str
    issuer: str
    serial_number: str
    valid_from: datetime
    valid_until: datetime


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _der_strings(raw: bytes) -> list[str]:
    """Extract common ASN.1 string values without adding another dependency."""
    values: list[str] = []

    def read_length(data: bytes, offset: int) -> tuple[int, int]:
        if offset >= len(data):
            return 0, offset
        first = data[offset]
        offset += 1
        if first < 0x80:
            return first, offset
        size = first & 0x7F
        if size == 0 or offset + size > len(data):
            return 0, len(data)
        length = int.from_bytes(data[offset : offset + size], "big")
        return length, offset + size

    def walk(data: bytes, start: int = 0, end: int | None = None) -> None:
        limit = len(data) if end is None else min(end, len(data))
        offset = start
        while offset + 2 <= limit:
            tag = data[offset]
            offset += 1
            length, content_start = read_length(data, offset)
            content_end = min(content_start + length, limit)
            if content_start > limit or content_end < content_start:
                break
            content = data[content_start:content_end]
            if tag in {0x0C, 0x13, 0x14, 0x16, 0x1E}:
                encodings = ["utf-8", "latin-1"]
                if tag == 0x1E:
                    encodings.insert(0, "utf-16-be")
                for encoding in encodings:
                    try:
                        text = content.decode(encoding).strip("\x00 ")
                    except UnicodeDecodeError:
                        continue
                    if text:
                        values.append(text)
                        break
            if tag & 0x20 or tag in {0x30, 0x31, 0xA0}:
                walk(content)
            offset = content_end

    walk(raw)
    for encoding in ("utf-8", "latin-1"):
        try:
            decoded = raw.decode(encoding, errors="ignore")
        except Exception:
            continue
        if decoded:
            values.append(decoded)
    return values


def _find_cnpj(certificate: x509.Certificate) -> str:
    candidates = [certificate.subject.rfc4514_string()]
    for attribute in certificate.subject:
        candidates.append(str(attribute.value))

    try:
        extension = certificate.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
        for value in extension:
            if isinstance(value, x509.OtherName):
                decoded_values = _der_strings(value.value)
                if value.type_id == CNPJ_OTHER_NAME_OID:
                    candidates = decoded_values + candidates
                else:
                    candidates.extend(decoded_values)
            else:
                candidates.append(str(getattr(value, "value", value)))
    except x509.ExtensionNotFound:
        pass

    for candidate in candidates:
        for match in re.findall(r"(?<!\d)(\d{14})(?!\d)", candidate):
            if len(match) == 14:
                return match
    raise ValueError("O CNPJ não foi encontrado dentro do certificado A1")


def _certificate_name(certificate: x509.Certificate) -> str:
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        values = certificate.subject.get_attributes_for_oid(oid)
        if not values:
            continue
        value = str(values[0].value).strip()
        value = re.sub(r"[:\s-]*\d{14}\s*$", "", value).strip()
        if value:
            return value
    return ""


def extract_certificate_company(path: str, password: str) -> CertificateCompany:
    key, certificate, _ = pkcs12.load_key_and_certificates(
        Path(path).read_bytes(),
        password.encode("utf-8") if password else None,
    )
    if key is None or certificate is None:
        raise ValueError("Certificado A1 inválido ou senha incorreta")
    return CertificateCompany(
        cnpj=_find_cnpj(certificate),
        legal_name=_certificate_name(certificate),
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        serial_number=str(certificate.serial_number),
        valid_from=certificate.not_valid_before_utc.replace(tzinfo=None),
        valid_until=certificate.not_valid_after_utc.replace(tzinfo=None),
    )


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _registry_from_cnpjws(data: dict[str, Any]) -> dict[str, str]:
    establishment = data.get("estabelecimento") or {}
    state = establishment.get("estado") or {}
    city = establishment.get("cidade") or {}
    uf = _first_text(state.get("sigla"))

    inscriptions = establishment.get("inscricoes_estaduais") or []
    active = [item for item in inscriptions if item.get("ativo") is True]
    same_state = [
        item
        for item in active
        if _first_text((item.get("estado") or {}).get("sigla")).upper() == uf.upper()
    ]
    selected_ie = (same_state or active or inscriptions)
    ie = _first_text(selected_ie[0].get("inscricao_estadual")) if selected_ie else ""

    street_type = _first_text(establishment.get("tipo_logradouro"))
    street_name = _first_text(establishment.get("logradouro"))
    address = " ".join(part for part in (street_type, street_name) if part).strip()
    phone = "".join(
        part
        for part in (
            _first_text(establishment.get("ddd1")),
            _first_text(establishment.get("telefone1")),
        )
        if part
    )

    return {
        "legal_name": _first_text(data.get("razao_social")),
        "trade_name": _first_text(establishment.get("nome_fantasia"), data.get("razao_social")),
        "cnpj": _digits(establishment.get("cnpj")),
        "state_registration": _digits(ie) or ie,
        "email": _first_text(establishment.get("email")),
        "phone": phone,
        "address": address,
        "number": _first_text(establishment.get("numero")),
        "complement": _first_text(establishment.get("complemento")),
        "district": _first_text(establishment.get("bairro")),
        "city": _first_text(city.get("nome")),
        "state": uf,
        "zip_code": _digits(establishment.get("cep")),
        "registry_status": _first_text(establishment.get("situacao_cadastral")),
        "registry_source": "CNPJ.ws",
    }


def _registry_from_brasilapi(data: dict[str, Any]) -> dict[str, str]:
    address = " ".join(
        part
        for part in (
            _first_text(data.get("descricao_tipo_de_logradouro"), data.get("tipo_logradouro")),
            _first_text(data.get("logradouro")),
        )
        if part
    ).strip()
    return {
        "legal_name": _first_text(data.get("razao_social")),
        "trade_name": _first_text(data.get("nome_fantasia"), data.get("razao_social")),
        "cnpj": _digits(data.get("cnpj")),
        "state_registration": "",
        "email": _first_text(data.get("email")),
        "phone": _first_text(data.get("ddd_telefone_1"), data.get("ddd_telefone_2")),
        "address": address,
        "number": _first_text(data.get("numero")),
        "complement": _first_text(data.get("complemento")),
        "district": _first_text(data.get("bairro")),
        "city": _first_text(data.get("municipio")),
        "state": _first_text(data.get("uf")),
        "zip_code": _digits(data.get("cep")),
        "registry_status": _first_text(data.get("descricao_situacao_cadastral")),
        "registry_source": "BrasilAPI",
    }


def fetch_company_registry(cnpj: str) -> dict[str, str]:
    digits = _digits(cnpj)
    if len(digits) != 14:
        raise ValueError("CNPJ inválido para consulta cadastral")

    errors: list[str] = []
    headers = {"Accept": "application/json", "User-Agent": "GestaoFacilERP/3.5"}
    with httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0), headers=headers) as client:
        try:
            response = client.get(CNPJWS_URL.format(cnpj=digits))
            response.raise_for_status()
            return _registry_from_cnpjws(response.json())
        except Exception as exc:
            errors.append(f"CNPJ.ws: {exc}")

        try:
            response = client.get(BRASILAPI_URL.format(cnpj=digits))
            response.raise_for_status()
            return _registry_from_brasilapi(response.json())
        except Exception as exc:
            errors.append(f"BrasilAPI: {exc}")

    raise ValueError("Não foi possível consultar os dados públicos do CNPJ. " + " | ".join(errors))
