from __future__ import annotations

import base64
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from email.header import decode_header
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.db.session import Base, SessionLocal, engine
from app.models import (
    Company,
    GmailImportLog,
    ImportedNfe,
    ImportedNfeItem,
    NfeInstallment,
    Payable,
    Product,
    StockMovement,
    Supplier,
)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
NFE_NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
XML_DIR = Path(os.environ.get("GMAIL_XML_STORAGE", "uploads/gmail_xml"))
XML_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ImportResult:
    access_key: str
    tenant_id: int
    company_id: int
    movements: int
    products_created: int
    payables_created: int
    duplicate: bool = False


def gmail_is_configured() -> bool:
    return all(
        os.environ.get(name, "").strip()
        for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    )


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _number(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", "."))
    except Exception:
        return Decimal("0")


def _date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path, NFE_NS)
    return (found.text or "").strip() if found is not None else default


def _decoded(value: str) -> str:
    output: list[str] = []
    for part, encoding in decode_header(value or ""):
        if isinstance(part, bytes):
            output.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            output.append(part)
    return "".join(output)


def _service():
    if not gmail_is_configured():
        raise RuntimeError(
            "Configure GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET e GMAIL_REFRESH_TOKEN no Railway."
        )
    credentials = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for item in labels:
        if item.get("name") == name:
            return item["id"]
    created = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


def _parts(payload: dict[str, Any]):
    yield payload
    for child in payload.get("parts", []) or []:
        yield from _parts(child)


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        item.get("name", "").lower(): item.get("value", "")
        for item in payload.get("headers", []) or []
    }


def _attachment(service, message_id: str, part: dict[str, Any]) -> bytes:
    body = part.get("body", {}) or {}
    data = body.get("data")
    if not data and body.get("attachmentId"):
        response = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=body["attachmentId"]
        ).execute()
        data = response.get("data")
    return base64.urlsafe_b64decode(data.encode()) if data else b""


def import_xml(db: Session, raw: bytes, filename: str) -> ImportResult:
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        raise ValueError(f"XML inválido: {exc}") from exc

    inf = root.find(".//nfe:infNFe", NFE_NS)
    if inf is None:
        raise ValueError("O arquivo XML não contém uma NF-e reconhecida.")

    access_key = (inf.attrib.get("Id") or "").replace("NFe", "")
    if len(access_key) != 44:
        access_key = _text(root.find(".//nfe:protNFe/nfe:infProt", NFE_NS), "nfe:chNFe")
    if len(access_key) != 44:
        raise ValueError("Não foi possível localizar a chave da NF-e.")

    existing = db.query(ImportedNfe).filter(ImportedNfe.access_key == access_key).first()
    if existing:
        return ImportResult(
            access_key=access_key,
            tenant_id=existing.tenant_id,
            company_id=existing.company_id,
            movements=0,
            products_created=0,
            payables_created=0,
            duplicate=True,
        )

    dest = inf.find("nfe:dest", NFE_NS)
    destination = _digits(_text(dest, "nfe:CNPJ") or _text(dest, "nfe:CPF"))
    companies = db.query(Company).filter(Company.cnpj == destination).all()
    if not companies:
        raise ValueError(f"Nenhuma empresa cadastrada possui o CNPJ destinatário {destination}.")
    if len(companies) > 1:
        raise ValueError(f"Existe mais de uma empresa cadastrada com o CNPJ {destination}.")
    company = companies[0]

    ide = inf.find("nfe:ide", NFE_NS)
    emit = inf.find("nfe:emit", NFE_NS)
    totals = inf.find("nfe:total/nfe:ICMSTot", NFE_NS)
    supplier_name = _text(emit, "nfe:xNome") or "Fornecedor importado"
    supplier_cnpj = _digits(_text(emit, "nfe:CNPJ"))
    issue_date = _date(_text(ide, "nfe:dhEmi") or _text(ide, "nfe:dEmi"))
    total_value = _number(_text(totals, "nfe:vNF"))

    safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename or f"{access_key}.xml")
    stored_path = XML_DIR / f"{access_key}_{safe_filename}"
    stored_path.write_bytes(raw)

    supplier = db.query(Supplier).filter(
        Supplier.tenant_id == company.tenant_id,
        Supplier.company_id == company.id,
        Supplier.cnpj == supplier_cnpj,
    ).first()
    if not supplier:
        db.add(Supplier(
            tenant_id=company.tenant_id,
            company_id=company.id,
            legal_name=supplier_name,
            cnpj=supplier_cnpj,
            state_registration=_text(emit, "nfe:IE"),
        ))

    nfe = ImportedNfe(
        tenant_id=company.tenant_id,
        company_id=company.id,
        access_key=access_key,
        invoice_number=_text(ide, "nfe:nNF"),
        series=_text(ide, "nfe:serie"),
        issue_date=issue_date,
        supplier_name=supplier_name,
        supplier_cnpj=supplier_cnpj,
        total_value=total_value,
        status="CONFIRMADA",
        filename=filename,
        stored_path=str(stored_path),
    )
    db.add(nfe)
    db.flush()

    movements = 0
    products_created = 0
    for det in inf.findall("nfe:det", NFE_NS):
        prod = det.find("nfe:prod", NFE_NS)
        if prod is None:
            continue
        code = _text(prod, "nfe:cProd")
        barcode = _text(prod, "nfe:cEAN")
        if barcode.upper() == "SEM GTIN":
            barcode = ""
        description = _text(prod, "nfe:xProd") or "Produto importado"
        quantity = _number(_text(prod, "nfe:qCom"))
        unit_value = _number(_text(prod, "nfe:vUnCom"))

        product = None
        if barcode:
            product = db.query(Product).filter(
                Product.tenant_id == company.tenant_id,
                Product.company_id == company.id,
                Product.barcode == barcode,
            ).first()
        if not product and code:
            product = db.query(Product).filter(
                Product.tenant_id == company.tenant_id,
                Product.company_id == company.id,
                Product.sku == code,
            ).first()
        if not product:
            product = Product(
                tenant_id=company.tenant_id,
                company_id=company.id,
                sku=code or f"XML-{nfe.id}-{det.attrib.get('nItem', '0')}",
                barcode=barcode,
                name=description,
                category="Importado do Gmail",
                unit=_text(prod, "nfe:uCom") or "UN",
                minimum_stock=0,
                unit_cost=unit_value,
                sale_price=Decimal("0"),
            )
            db.add(product)
            db.flush()
            products_created += 1
        else:
            product.unit_cost = unit_value

        db.add(ImportedNfeItem(
            nfe_id=nfe.id,
            product_code=code,
            barcode=barcode,
            description=description,
            ncm=_text(prod, "nfe:NCM"),
            cfop=_text(prod, "nfe:CFOP"),
            unit=_text(prod, "nfe:uCom") or "UN",
            invoiced_quantity=quantity,
            received_quantity=quantity,
            unit_value=unit_value,
            total_value=_number(_text(prod, "nfe:vProd")),
            matched_product_id=product.id,
        ))

        movement_qty = int(quantity)
        if movement_qty > 0:
            db.add(StockMovement(
                tenant_id=company.tenant_id,
                company_id=company.id,
                product_id=product.id,
                movement_type="ENTRADA",
                quantity=movement_qty,
                unit_value=unit_value,
                document=f"NFE-{access_key}",
                movement_date=issue_date or date.today(),
            ))
            movements += 1

    payables_created = 0
    duplicates = inf.findall(".//nfe:cobr/nfe:dup", NFE_NS)
    for dup in duplicates:
        due = _date(_text(dup, "nfe:dVenc")) or issue_date or date.today()
        value = _number(_text(dup, "nfe:vDup"))
        db.add(NfeInstallment(
            nfe_id=nfe.id,
            installment_number=_text(dup, "nfe:nDup"),
            due_date=due,
            value=value,
        ))
        db.add(Payable(
            tenant_id=company.tenant_id,
            company_id=company.id,
            supplier=supplier_name,
            due_date=due,
            value=value,
            status="EM ABERTO",
        ))
        payables_created += 1

    if not duplicates and total_value > 0:
        db.add(Payable(
            tenant_id=company.tenant_id,
            company_id=company.id,
            supplier=supplier_name,
            due_date=issue_date or date.today(),
            value=total_value,
            status="EM ABERTO",
        ))
        payables_created = 1

    db.commit()
    return ImportResult(
        access_key=access_key,
        tenant_id=company.tenant_id,
        company_id=company.id,
        movements=movements,
        products_created=products_created,
        payables_created=payables_created,
    )


def sync_once() -> dict[str, int]:
    Base.metadata.create_all(bind=engine)
    service = _service()
    imported_name = os.environ.get("GMAIL_IMPORTED_LABEL", "GESTAO_FACIL_IMPORTADO")
    error_name = os.environ.get("GMAIL_ERROR_LABEL", "GESTAO_FACIL_ERRO")
    imported_label = _label(service, imported_name)
    error_label = _label(service, error_name)
    lookback = max(int(os.environ.get("GMAIL_LOOKBACK_DAYS", "30")), 1)
    maximum = max(int(os.environ.get("GMAIL_MAX_MESSAGES", "100")), 1)
    query = os.environ.get(
        "GMAIL_SEARCH_QUERY",
        f"has:attachment filename:xml newer_than:{lookback}d -label:{imported_name} -label:{error_name}",
    )
    messages = service.users().messages().list(
        userId="me", q=query, maxResults=maximum
    ).execute().get("messages", []) or []

    stats = {"found": len(messages), "messages_imported": 0, "xml_imported": 0, "duplicates": 0, "errors": 0}
    for summary in messages:
        message_id = summary["id"]
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = message.get("payload", {}) or {}
        headers = _headers(payload)
        sender = _decoded(headers.get("from", ""))
        subject = _decoded(headers.get("subject", ""))
        db = SessionLocal()
        try:
            if db.query(GmailImportLog).filter(
                GmailImportLog.gmail_message_id == message_id,
                GmailImportLog.status == "IMPORTADO",
            ).first():
                service.users().messages().modify(
                    userId="me", id=message_id, body={"addLabelIds": [imported_label]}
                ).execute()
                continue

            attachments: list[tuple[str, bytes]] = []
            for part in _parts(payload):
                filename = _decoded(part.get("filename", ""))
                mime = (part.get("mimeType") or "").lower()
                if filename.lower().endswith(".xml") or mime in ("application/xml", "text/xml"):
                    raw = _attachment(service, message_id, part)
                    if raw:
                        attachments.append((filename or "nota.xml", raw))
            if not attachments:
                raise ValueError("Nenhum anexo XML válido encontrado.")

            details=[]
            tenant_id=None
            company_id=None
            access_key=""
            for filename, raw in attachments:
                result=import_xml(db, raw, filename)
                tenant_id=result.tenant_id
                company_id=result.company_id
                access_key=result.access_key
                if result.duplicate:
                    stats["duplicates"] += 1
                    details.append(f"{filename}: duplicada")
                else:
                    stats["xml_imported"] += 1
                    details.append(
                        f"{filename}: {result.movements} entradas, {result.products_created} produtos novos, {result.payables_created} contas"
                    )

            db.add(GmailImportLog(
                gmail_message_id=message_id,
                gmail_thread_id=message.get("threadId", ""),
                tenant_id=tenant_id,
                company_id=company_id,
                sender=sender,
                subject=subject,
                filename=", ".join(name for name, _ in attachments),
                access_key=access_key,
                status="IMPORTADO",
                detail="; ".join(details),
            ))
            db.commit()
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [imported_label], "removeLabelIds": ["UNREAD"]},
            ).execute()
            stats["messages_imported"] += 1
        except Exception as exc:
            db.rollback()
            db.add(GmailImportLog(
                gmail_message_id=message_id,
                gmail_thread_id=message.get("threadId", ""),
                sender=sender,
                subject=subject,
                status="ERRO",
                detail=str(exc)[:4000],
            ))
            db.commit()
            service.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [error_label]}
            ).execute()
            stats["errors"] += 1
        finally:
            db.close()
    return stats
