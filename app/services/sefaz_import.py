from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (
    Company,
    ImportedNfe,
    ImportedNfeItem,
    NfeInstallment,
    Payable,
    Product,
    SefazDistributionDocument,
    StockMovement,
    Supplier,
)

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path, NS)
    return (found.text or "").strip() if found is not None else default


def _decimal(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", "."))
    except Exception:
        return Decimal("0")


def _date(value: str | None) -> date | None:
    if not value:
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except Exception:
            continue
    return None


def _is_full_nfe(root: ET.Element) -> bool:
    return root.find(".//nfe:infNFe", NS) is not None


def import_sefaz_document(
    db: Session,
    document: SefazDistributionDocument,
    tenant_id: int,
    company_id: int,
) -> dict:
    """Importa um XML completo recebido pela Distribuição DF-e.

    Resumos (resNFe) permanecem aguardando manifestação e não movimentam estoque.
    A chave da NF-e garante idempotência e impede duplicidade.
    """
    if document.tenant_id != tenant_id or document.company_id != company_id:
        raise ValueError("Documento não pertence à empresa selecionada.")

    xml_path = Path(document.xml_path)
    if not xml_path.exists():
        raise ValueError("O XML salvo não foi encontrado no servidor.")

    raw = xml_path.read_bytes()
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        document.status = "ERRO"
        db.commit()
        raise ValueError(f"XML inválido: {exc}") from exc

    if not _is_full_nfe(root):
        document.status = "AGUARDANDO_MANIFESTACAO"
        db.commit()
        return {
            "ok": False,
            "status": document.status,
            "message": "A SEFAZ disponibilizou apenas o resumo. É necessário manifestar a nota para liberar o XML completo.",
        }

    inf_nfe = root.find(".//nfe:infNFe", NS)
    assert inf_nfe is not None

    access_key = (inf_nfe.attrib.get("Id") or "").replace("NFe", "")
    if len(access_key) != 44:
        access_key = _text(root, ".//nfe:protNFe/nfe:infProt/nfe:chNFe")
    if len(access_key) != 44:
        document.status = "ERRO"
        db.commit()
        raise ValueError("Não foi possível localizar a chave da NF-e.")

    existing = db.query(ImportedNfe).filter(ImportedNfe.access_key == access_key).first()
    if existing:
        document.status = "DUPLICADA"
        document.access_key = access_key
        db.commit()
        return {
            "ok": True,
            "duplicate": True,
            "status": document.status,
            "nfe_id": existing.id,
            "access_key": access_key,
        }

    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == tenant_id,
    ).first()
    if not company:
        raise ValueError("Empresa não encontrada.")

    dest = inf_nfe.find("nfe:dest", NS)
    destination_document = _digits(_text(dest, "nfe:CNPJ") or _text(dest, "nfe:CPF"))
    company_document = _digits(company.cnpj)
    if destination_document and company_document and destination_document != company_document:
        document.status = "ERRO_CNPJ"
        db.commit()
        raise ValueError(
            f"O destinatário do XML ({destination_document}) não corresponde ao CNPJ da empresa ({company_document})."
        )

    ide = inf_nfe.find("nfe:ide", NS)
    emit = inf_nfe.find("nfe:emit", NS)
    total_node = inf_nfe.find("nfe:total/nfe:ICMSTot", NS)

    supplier_name = _text(emit, "nfe:xNome") or "Fornecedor importado"
    supplier_trade_name = _text(emit, "nfe:xFant")
    supplier_cnpj = _digits(_text(emit, "nfe:CNPJ"))
    issue_date = _date(_text(ide, "nfe:dhEmi") or _text(ide, "nfe:dEmi"))
    total_value = _decimal(_text(total_node, "nfe:vNF"))

    supplier = db.query(Supplier).filter(
        Supplier.tenant_id == tenant_id,
        Supplier.company_id == company_id,
        Supplier.cnpj == supplier_cnpj,
    ).first()
    supplier_created = False
    if not supplier:
        supplier = Supplier(
            tenant_id=tenant_id,
            company_id=company_id,
            legal_name=supplier_name,
            trade_name=supplier_trade_name,
            cnpj=supplier_cnpj,
            state_registration=_text(emit, "nfe:IE"),
        )
        db.add(supplier)
        supplier_created = True

    nfe = ImportedNfe(
        tenant_id=tenant_id,
        company_id=company_id,
        access_key=access_key,
        invoice_number=_text(ide, "nfe:nNF"),
        series=_text(ide, "nfe:serie"),
        issue_date=issue_date,
        supplier_name=supplier_name,
        supplier_cnpj=supplier_cnpj,
        total_value=total_value,
        status="CONFIRMADA",
        filename=xml_path.name,
        stored_path=str(xml_path),
    )
    db.add(nfe)
    db.flush()

    products_created = 0
    products_matched = 0
    stock_movements = 0

    for det in inf_nfe.findall("nfe:det", NS):
        prod = det.find("nfe:prod", NS)
        if prod is None:
            continue

        product_code = _text(prod, "nfe:cProd")
        barcode = _text(prod, "nfe:cEAN")
        if barcode.upper() == "SEM GTIN":
            barcode = ""
        description = _text(prod, "nfe:xProd") or "Produto importado da NF-e"
        quantity = _decimal(_text(prod, "nfe:qCom"))
        unit_value = _decimal(_text(prod, "nfe:vUnCom"))
        item_total = _decimal(_text(prod, "nfe:vProd"))

        product = None
        if barcode:
            product = db.query(Product).filter(
                Product.tenant_id == tenant_id,
                Product.company_id == company_id,
                Product.barcode == barcode,
            ).first()
        if not product and product_code:
            product = db.query(Product).filter(
                Product.tenant_id == tenant_id,
                Product.company_id == company_id,
                Product.sku == product_code,
            ).first()

        if not product:
            product = Product(
                tenant_id=tenant_id,
                company_id=company_id,
                sku=product_code or f"SEFAZ-{nfe.id}-{det.attrib.get('nItem', '0')}",
                barcode=barcode,
                name=description,
                category="Importado da SEFAZ",
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
            products_matched += 1

        db.add(
            ImportedNfeItem(
                nfe_id=nfe.id,
                product_code=product_code,
                barcode=barcode,
                description=description,
                ncm=_text(prod, "nfe:NCM"),
                cfop=_text(prod, "nfe:CFOP"),
                unit=_text(prod, "nfe:uCom") or "UN",
                invoiced_quantity=quantity,
                received_quantity=quantity,
                unit_value=unit_value,
                total_value=item_total,
                matched_product_id=product.id,
            )
        )

        movement_quantity = int(quantity)
        if movement_quantity > 0:
            db.add(
                StockMovement(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    product_id=product.id,
                    movement_type="ENTRADA",
                    quantity=movement_quantity,
                    unit_value=unit_value,
                    document=f"NFE-{access_key}",
                    movement_date=issue_date or date.today(),
                )
            )
            stock_movements += 1

    payables_created = 0
    installments = inf_nfe.findall(".//nfe:cobr/nfe:dup", NS)
    for dup in installments:
        due_date = _date(_text(dup, "nfe:dVenc"))
        value = _decimal(_text(dup, "nfe:vDup"))
        db.add(
            NfeInstallment(
                nfe_id=nfe.id,
                installment_number=_text(dup, "nfe:nDup"),
                due_date=due_date,
                value=value,
            )
        )
        db.add(
            Payable(
                tenant_id=tenant_id,
                company_id=company_id,
                supplier=supplier_name,
                due_date=due_date or issue_date or date.today(),
                value=value,
                status="EM ABERTO",
            )
        )
        payables_created += 1

    if not installments and total_value > 0:
        db.add(
            Payable(
                tenant_id=tenant_id,
                company_id=company_id,
                supplier=supplier_name,
                due_date=issue_date or date.today(),
                value=total_value,
                status="EM ABERTO",
            )
        )
        payables_created = 1

    document.status = "IMPORTADO"
    document.access_key = access_key
    db.commit()

    return {
        "ok": True,
        "duplicate": False,
        "status": document.status,
        "nfe_id": nfe.id,
        "access_key": access_key,
        "supplier_created": supplier_created,
        "products_created": products_created,
        "products_matched": products_matched,
        "stock_movements": stock_movements,
        "payables_created": payables_created,
    }
