from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_ofx_date(value: str) -> date:
    text = digits(value)[:8]
    return datetime.strptime(text, "%Y%m%d").date()


def parse_ofx(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8", errors="ignore")
    xml_start = text.find("<OFX")
    if xml_start < 0:
        xml_start = text.find("<ofx")
    if xml_start < 0:
        raise ValueError("Arquivo OFX inválido")
    xml_text = text[xml_start:]
    xml_text = re.sub(
        r"<([A-Z0-9_]+)>([^<\r\n]+)(?=\r?\n|<)",
        r"<\1>\2</\1>",
        xml_text,
        flags=re.I,
    )
    root = ET.fromstring(xml_text)
    rows = []
    for node in root.findall(".//STMTTRN"):
        trn_type = (node.findtext("TRNTYPE") or "").upper()
        amount = Decimal(node.findtext("TRNAMT") or "0")
        fitid = node.findtext("FITID") or ""
        memo = node.findtext("MEMO") or node.findtext("NAME") or ""
        checknum = node.findtext("CHECKNUM") or ""
        dt = _parse_ofx_date(node.findtext("DTPOSTED") or "")
        rows.append({
            "external_id": fitid or f"{dt.isoformat()}-{amount}-{memo[:40]}",
            "transaction_date": dt,
            "description": memo.strip(),
            "document_number": checknum.strip(),
            "amount": abs(amount),
            "transaction_type": "CREDITO" if amount >= 0 else "DEBITO",
            "category": trn_type,
            "counterparty_name": "",
            "counterparty_document": "",
            "raw": {
                "type": trn_type,
                "memo": memo,
                "fitid": fitid,
            },
        })
    return rows


def parse_cnab(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("latin-1", errors="ignore")
    lines = [line.rstrip("\r\n") for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Arquivo CNAB vazio")
    width = max(len(line) for line in lines)
    if width not in {240, 400}:
        raise ValueError(
            "CNAB não reconhecido. Este importador aceita arquivos de 240 ou 400 posições."
        )

    rows = []
    for index, line in enumerate(lines, start=1):
        if width == 240:
            if len(line) < 240 or line[7:8] != "3":
                continue
            segment = line[13:14]
            if segment not in {"A", "J", "O"}:
                continue
            amount_text = digits(line[119:134] or line[81:96])
            date_text = digits(line[93:101] or line[73:81])
            if len(date_text) != 8 or not amount_text:
                continue
            dt = datetime.strptime(date_text, "%d%m%Y").date()
            amount = Decimal(amount_text) / Decimal("100")
            document = line[73:93].strip()
            description = line[43:73].strip() or f"CNAB 240 segmento {segment}"
        else:
            if len(line) < 400 or line[0:1] != "1":
                continue
            amount_text = digits(line[253:266])
            date_text = digits(line[110:116])
            if len(date_text) != 6 or not amount_text:
                continue
            dt = datetime.strptime(date_text, "%d%m%y").date()
            amount = Decimal(amount_text) / Decimal("100")
            document = line[62:72].strip()
            description = line[46:76].strip() or "CNAB 400"

        rows.append({
            "external_id": f"CNAB-{width}-{index}-{document}-{amount}",
            "transaction_date": dt,
            "description": description,
            "document_number": document,
            "amount": amount,
            "transaction_type": "DEBITO",
            "category": f"CNAB_{width}",
            "counterparty_name": "",
            "counterparty_document": "",
            "raw": {"line": index, "width": width},
        })
    return rows


def parse_statement(filename: str, content: bytes) -> tuple[str, list[dict[str, Any]]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".ofx":
        return "OFX", parse_ofx(content)
    if suffix in {".txt", ".ret", ".rem", ".cnab"}:
        return "CNAB", parse_cnab(content)
    raise ValueError("Formato não suportado. Envie OFX, TXT, RET, REM ou CNAB.")


def analyze_match(
    *,
    transaction_type: str,
    amount: Decimal,
    description: str,
    payable_found: bool,
    receivable_found: bool,
    dda_found: bool,
) -> dict[str, Any]:
    score = 0
    reasons = []
    if transaction_type == "DEBITO":
        if payable_found:
            score += 70
            reasons.append("Conta a pagar com mesmo valor localizada.")
        if dda_found:
            score += 25
            reasons.append("Boleto DDA compatível localizado.")
    else:
        if receivable_found:
            score += 80
            reasons.append("Conta a receber com mesmo valor localizada.")
    if amount > 0:
        score += 5
    recommendation = (
        "Conciliar automaticamente."
        if score >= 80
        else "Revisar antes de conciliar."
        if score >= 50
        else "Sem correspondência suficiente; analisar manualmente."
    )
    return {
        "score": min(score, 100),
        "recommendation": recommendation,
        "reasons": reasons,
    }
