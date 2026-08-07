import re
import math
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from datetime import date
from threading import Lock
from decimal import Decimal
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr
from sqlalchemy import case, func
from sqlalchemy.orm import Session
from pypdf import PdfReader
from cryptography.fernet import Fernet
import base64
import xml.etree.ElementTree as ET
import json

from app.core.security import create_token, decode_token, hash_password, verify_password
from app.db.session import Base, engine, get_db
from app.models import Company, Customer, FiscalConfig, FiscalDocument, GmailImportLog, ImportedNfe, ImportedNfeItem, ImportedPdf, NfeInstallment, Payable, Product, Receivable, Sale, SaleItem, StockMovement, Supplier, Tenant, ShopeeShop, ShopeeOrder, ShopeeSyncLog, DdaConnector, DdaBoleto, DdaSyncLog, BankStatementImport, BankTransaction, BankReconciliationLog, PagBankConfig, PagBankPayment, PagBankWebhookLog, User
from app.services.gmail_nfe_import import gmail_is_configured, sync_once
from app.services.ai_assistant import answer_question, build_company_context
from app.services.shopee_api import authorization_url as shopee_authorization_url, configured as shopee_configured, exchange_code as shopee_exchange_code, refresh_token as shopee_refresh_token, shop_get as shopee_shop_get
from app.services.dda_connectors import SUPPORTED_PROVIDERS as DDA_SUPPORTED_PROVIDERS, analyze_boleto as dda_analyze_boleto, fetch_boletos as dda_fetch_boletos, provider_catalog as dda_provider_catalog
from app.services.bank_reconciliation import analyze_match as bank_analyze_match, file_hash as bank_file_hash, parse_statement as bank_parse_statement
from app.services.company_registry import extract_certificate_company, fetch_company_registry
from app.services.pagbank_api import create_boleto_order as pagbank_create_boleto_order, create_pix_order as pagbank_create_pix_order, extract_payment_details as pagbank_extract_payment_details, get_order as pagbank_get_order, paid_status as pagbank_paid_status, test_connection as pagbank_test_connection

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Gestão Fácil API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

# Impede duas consultas simultâneas do mesmo CNPJ nesta instância.
_sefaz_query_locks: dict[tuple[int, int], Lock] = {}
_sefaz_query_locks_guard = Lock()
_SEFAZ_COOLDOWN = timedelta(hours=1)

def _sefaz_lock(tenant_id: int, company_id: int) -> Lock:
    key = (tenant_id, company_id)
    with _sefaz_query_locks_guard:
        return _sefaz_query_locks.setdefault(key, Lock())

def _normalized_nsu(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")[-15:]
    return digits.zfill(15)

def _remaining_sefaz_cooldown(config: "SefazDistributionConfig", now: datetime) -> int:
    if not config.last_query_at:
        return 0
    last_nsu = _normalized_nsu(config.last_nsu)
    max_nsu = _normalized_nsu(config.max_nsu)
    must_wait = config.last_status_code in {"137", "656"}
    if max_nsu != "000000000000000" and last_nsu == max_nsu:
        must_wait = True
    if not must_wait:
        return 0
    elapsed = now - config.last_query_at
    return max(0, int((_SEFAZ_COOLDOWN - elapsed).total_seconds()))

class RegisterIn(BaseModel):
    company_name: str
    owner_name: str
    email: EmailStr
    password: str



class AiQuestionIn(BaseModel):
    company_id: int
    question: str
    history: list[dict[str, str]] = []

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class ProductIn(BaseModel):
    sku: str
    barcode: str = ""
    name: str
    category: str = "Outros"
    unit: str = "UN"
    minimum_stock: int = 0
    unit_cost: Decimal = Decimal("0")
    sale_price: Decimal = Decimal("0")

class CompanyIn(BaseModel):
    trade_name: str
    legal_name: str = ""
    cnpj: str = ""
    state_registration: str = ""
    municipal_registration: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    number: str = ""
    complement: str = ""
    district: str = ""
    city: str = ""
    state: str = "SP"
    zip_code: str = ""


class SaleItemIn(BaseModel):
    product_id: int
    quantity: int
    unit_price: Decimal

class SaleIn(BaseModel):
    company_id: int
    customer_id: int | None = None
    customer_name: str = "Consumidor final"
    customer_document: str = ""
    customer_person_type: str = "PF"
    customer_state_registration: str = ""
    payment_method: str = "PIX"
    discount: Decimal = Decimal("0")
    notes: str = ""
    create_receivable: bool = False
    due_date: date | None = None
    issue_invoice: bool = False
    items: list[SaleItemIn]

class ReceivableIn(BaseModel):
    company_id: int
    customer: str
    description: str = ""
    due_date: date
    value: Decimal

class PdfConfirmIn(BaseModel):
    supplier: str = ""
    supplier_document: str = ""
    document_number: str = ""
    issue_date: date | None = None
    due_date: date | None = None
    total_value: Decimal = Decimal("0")
    barcode: str = ""
    create_payable: bool = True


class FiscalSettingsIn(BaseModel):
    provider: str = "NUVEM_FISCAL"
    environment: str = "HOMOLOGACAO"
    client_id: str = ""
    client_secret: str = ""
    automatic_issue: bool = False
    series: str = "1"
    last_number: int = 0


class CustomerIn(BaseModel):
    company_id: int
    person_type: str = "PF"
    name: str
    trade_name: str = ""
    document: str = ""
    state_registration: str = ""
    email: str = ""
    phone: str = ""
    zip_code: str = ""
    address: str = ""
    number: str = ""
    complement: str = ""
    district: str = ""
    city: str = ""
    state: str = "SP"

class ConfirmNfeItemIn(BaseModel):
    item_id: int
    product_id: int | None = None
    received_quantity: Decimal
    create_product: bool = True

class ConfirmNfeIn(BaseModel):
    items: list[ConfirmNfeItemIn]
    create_payables: bool = True

class DetailedSaleIn(BaseModel):
    company_id: int
    customer_id: int | None = None
    customer_name: str = "Consumidor final"
    customer_document: str = ""
    customer_person_type: str = "PF"
    payment_method: str = "PIX"
    discount: Decimal = Decimal("0")
    notes: str = ""
    create_receivable: bool = False
    due_date: date | None = None
    items: list[SaleItemIn]

def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    try:
        payload = decode_token(credentials.credentials)
        user = db.get(User, int(payload["sub"]))
    except Exception:
        raise HTTPException(401, "Token inválido")
    if not user or not user.active:
        raise HTTPException(401, "Usuário inválido")
    return user

def stock_of(db: Session, tenant_id: int, company_id: int, product_id: int) -> int:
    total = db.query(
        func.coalesce(
            func.sum(
                case(
                    (StockMovement.movement_type == "ENTRADA", StockMovement.quantity),
                    else_=-StockMovement.quantity,
                )
            ), 0
        )
    ).filter(
        StockMovement.tenant_id == tenant_id,
        StockMovement.company_id == company_id,
        StockMovement.product_id == product_id,
    ).scalar()
    return int(total or 0)

@app.post("/api/auth/register")
def register(data: RegisterIn, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email.lower()).first():
        raise HTTPException(409, "E-mail já cadastrado")
    slug = re.sub(r"[^a-z0-9]+", "-", data.company_name.lower()).strip("-")
    tenant = Tenant(name=data.company_name, slug=f"{slug}-{db.query(Tenant).count()+1}")
    db.add(tenant)
    db.flush()
    company = Company(
        tenant_id=tenant.id,
        trade_name=data.company_name,
        legal_name=data.company_name,
    )
    db.add(company)
    user = User(
        tenant_id=tenant.id,
        name=data.owner_name,
        email=data.email.lower(),
        password_hash=hash_password(data.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": create_token(user.id, tenant.id), "token_type": "bearer"}

@app.post("/api/auth/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email.lower()).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "E-mail ou senha inválidos")
    return {"access_token": create_token(user.id, user.tenant_id), "token_type": "bearer"}

@app.get("/api/companies")
def companies(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.query(Company).filter(Company.tenant_id == user.tenant_id).all()

@app.put("/api/companies/{company_id}")
def update_company(company_id: int, data: CompanyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")
    for key, value in data.model_dump().items():
        setattr(company, key, value)
    db.commit()
    return company

@app.get("/api/products")
def products(company_id: int, search: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = db.query(Product).filter(
        Product.tenant_id == user.tenant_id,
        Product.company_id == company_id,
    )
    if search:
        term = f"%{search}%"
        query = query.filter(
            Product.name.ilike(term) |
            Product.sku.ilike(term) |
            Product.barcode.ilike(term)
        )
    result = []
    for p in query.order_by(Product.name).all():
        result.append({
            "id": p.id,
            "sku": p.sku,
            "barcode": p.barcode,
            "name": p.name,
            "category": p.category,
            "unit": p.unit,
            "minimum_stock": p.minimum_stock,
            "unit_cost": float(p.unit_cost),
            "sale_price": float(p.sale_price),
            "current_stock": stock_of(db, user.tenant_id, company_id, p.id),
        })
    return result

@app.post("/api/products")
def create_product(company_id: int, data: ProductIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    product = Product(
        tenant_id=user.tenant_id,
        company_id=company_id,
        **data.model_dump(),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product

@app.post("/api/stock/adjust")
def adjust_stock(company_id: int, product_id: int, counted_stock: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    current = stock_of(db, user.tenant_id, company_id, product_id)
    difference = counted_stock - current
    if difference != 0:
        db.add(StockMovement(
            tenant_id=user.tenant_id,
            company_id=company_id,
            product_id=product_id,
            movement_type="ENTRADA" if difference > 0 else "SAÍDA",
            quantity=abs(difference),
            document="AJUSTE",
        ))
        db.commit()
    return {"stock": counted_stock}

@app.post("/api/sales")
def create_sale(
    data: SaleIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not data.items:
        raise HTTPException(400, "Inclua pelo menos um produto")

    customer = None
    if data.customer_id:
        customer = db.query(Customer).filter(
            Customer.id == data.customer_id,
            Customer.tenant_id == user.tenant_id,
            Customer.company_id == data.company_id,
        ).first()
        if not customer:
            raise HTTPException(404, "Cliente não encontrado")

    checked = []
    subtotal = Decimal("0")
    for item in data.items:
        product = db.query(Product).filter(
            Product.id == item.product_id,
            Product.tenant_id == user.tenant_id,
            Product.company_id == data.company_id,
        ).first()
        if not product:
            raise HTTPException(404, "Produto não encontrado")

        available = stock_of(db, user.tenant_id, data.company_id, product.id)
        if item.quantity <= 0:
            raise HTTPException(400, "Quantidade inválida")
        if item.quantity > available:
            raise HTTPException(
                400,
                f"Estoque insuficiente para {product.name}. Disponível: {available}",
            )
        if item.unit_price < 0:
            raise HTTPException(400, "Preço inválido")

        item_total = item.unit_price * item.quantity
        subtotal += item_total
        checked.append((product, item, item_total))

    discount = max(data.discount, Decimal("0"))
    total = max(subtotal - discount, Decimal("0"))
    count = db.query(Sale).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == data.company_id,
    ).count()

    customer_name = customer.name if customer else (data.customer_name or "Consumidor final")
    customer_document = customer.document if customer else re.sub(r"\D", "", data.customer_document)
    customer_person_type = customer.person_type if customer else data.customer_person_type.upper()
    customer_ie = customer.state_registration if customer else data.customer_state_registration

    sale = Sale(
        tenant_id=user.tenant_id,
        company_id=data.company_id,
        number=f"VENDA-{count + 1:06d}",
        customer_id=customer.id if customer else None,
        customer_name=customer_name,
        customer_document=customer_document,
        customer_person_type=customer_person_type,
        customer_state_registration=customer_ie,
        payment_method=data.payment_method,
        discount=discount,
        notes=data.notes,
        total=total,
        sale_date=date.today(),
        status="CONCLUÍDA",
    )
    db.add(sale)
    db.flush()

    for product, item, item_total in checked:
        db.add(SaleItem(
            sale_id=sale.id,
            product_id=product.id,
            quantity=item.quantity,
            unit_price=item.unit_price,
            total=item_total,
        ))
        db.add(StockMovement(
            tenant_id=user.tenant_id,
            company_id=data.company_id,
            product_id=product.id,
            movement_type="SAÍDA",
            quantity=item.quantity,
            unit_value=item.unit_price,
            document=sale.number,
            movement_date=date.today(),
        ))

    if data.create_receivable:
        db.add(Receivable(
            tenant_id=user.tenant_id,
            company_id=data.company_id,
            customer=customer_name,
            description=f"Venda {sale.number}",
            due_date=data.due_date or date.today(),
            value=total,
            status="EM ABERTO",
            sale_id=sale.id,
        ))

    invoice_status = "NÃO SOLICITADA"
    if data.issue_invoice:
        config = db.query(FiscalConfig).filter(
            FiscalConfig.tenant_id == user.tenant_id,
            FiscalConfig.company_id == data.company_id,
        ).first()
        company = db.query(Company).filter(
            Company.id == data.company_id,
            Company.tenant_id == user.tenant_id,
        ).first()

        company_ready = bool(
            company and company.cnpj and company.state_registration
            and company.legal_name and company.address and company.city and company.state
        )
        fiscal_ready = bool(
            config and config.client_id_encrypted and config.client_secret_encrypted
            and config.certificate_path
        )

        if company_ready and fiscal_ready:
            invoice_status = "AGUARDANDO TRANSMISSÃO"
            error_message = ""
        else:
            invoice_status = "PENDENTE DE CONFIGURAÇÃO"
            error_message = (
                "Complete empresa, certificado A1 e credenciais do provedor fiscal."
            )

        db.add(FiscalDocument(
            tenant_id=user.tenant_id,
            company_id=data.company_id,
            sale_id=sale.id,
            document_type="NFE",
            environment=config.environment if config else "HOMOLOGACAO",
            status=invoice_status,
            error_message=error_message,
        ))

    db.commit()
    db.refresh(sale)
    return {
        "id": sale.id,
        "number": sale.number,
        "customer_name": sale.customer_name,
        "total": float(sale.total),
        "sale_date": sale.sale_date,
        "status": sale.status,
        "invoice_status": invoice_status,
    }


@app.get("/api/sales")
def sales(company_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(Sale).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
    ).order_by(Sale.id.desc()).all()
    return [
        {
            "id": s.id,
            "number": s.number,
            "customer_name": s.customer_name,
            "total": float(s.total),
            "sale_date": s.sale_date,
            "status": s.status,
        }
        for s in rows
    ]

@app.get("/api/payables")
def payables(company_id: int, year: int, month: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.query(Payable).filter(
        Payable.tenant_id == user.tenant_id,
        Payable.company_id == company_id,
        func.extract("year", Payable.due_date) == year,
        func.extract("month", Payable.due_date) == month,
    ).all()


@app.post("/api/payables")
def create_payable(
    data: ReceivableIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = Payable(
        tenant_id=user.tenant_id,
        company_id=data.company_id,
        supplier=data.customer,
        due_date=data.due_date,
        value=data.value,
        status="EM ABERTO",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.get("/api/receivables")
def list_receivables(
    company_id: int,
    year: int,
    month: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(Receivable).filter(
        Receivable.tenant_id == user.tenant_id,
        Receivable.company_id == company_id,
        func.extract("year", Receivable.due_date) == year,
        func.extract("month", Receivable.due_date) == month,
    ).order_by(Receivable.due_date).all()
    return [
        {
            "id": r.id,
            "customer": r.customer,
            "description": r.description,
            "due_date": r.due_date,
            "value": float(r.value),
            "status": r.status,
            "received_date": r.received_date,
        }
        for r in rows
    ]


@app.post("/api/receivables")
def create_receivable(
    data: ReceivableIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = Receivable(
        tenant_id=user.tenant_id,
        company_id=data.company_id,
        customer=data.customer,
        description=data.description,
        due_date=data.due_date,
        value=data.value,
        status="EM ABERTO",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/receivables/{receivable_id}/receive")
def receive_receivable(
    receivable_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(Receivable).filter(
        Receivable.id == receivable_id,
        Receivable.tenant_id == user.tenant_id,
    ).first()
    if not row:
        raise HTTPException(404, "Conta a receber não encontrada")
    row.status = "RECEBIDO"
    row.received_date = date.today()
    db.commit()
    return {"ok": True}


UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _parse_money(value: str) -> Decimal:
    clean = (value or "").strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(clean)
    except Exception:
        return Decimal("0")


def _parse_date(value: str):
    value = (value or "").strip()
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except Exception:
            continue
    return None


def extract_pdf_fields(text: str, document_type: str) -> dict:
    normalized = " ".join((text or "").split())

    cnpj_match = re.search(r"(?:CNPJ|CPF)[:\s]*([0-9./-]{11,18})", normalized, re.I)
    number_match = re.search(
        r"(?:N[º°o.]?\s*(?:da\s*)?(?:NF-e|Nota Fiscal|Documento)|Número do documento|Nosso Número)[:\s]*([A-Z0-9./-]{3,30})",
        normalized,
        re.I,
    )

    total = Decimal("0")
    for pattern in (
        r"(?:Valor Total da Nota|Valor Total|Total a Pagar|Valor do Documento|Valor Cobrado)[:\sR$]*([0-9.]+,[0-9]{2})",
        r"R\$\s*([0-9.]+,[0-9]{2})",
    ):
        values = re.findall(pattern, normalized, re.I)
        if values:
            total = max((_parse_money(v) for v in values), default=Decimal("0"))
            if total:
                break

    due_match = re.search(r"(?:Vencimento|Data de Vencimento)[:\s]*(\d{2}[/-]\d{2}[/-]\d{4})", normalized, re.I)
    issue_match = re.search(r"(?:Data de Emissão|Emissão)[:\s]*(\d{2}[/-]\d{2}[/-]\d{4})", normalized, re.I)

    supplier = ""
    supplier_match = re.search(
        r"(?:Beneficiário|Cedente|Fornecedor|Emitente)[:\s]+(.{3,120}?)(?=\s(?:CNPJ|CPF|Vencimento|Valor|Número|Data)|$)",
        normalized,
        re.I,
    )
    if supplier_match:
        supplier = supplier_match.group(1).strip(" -:")

    barcode = ""
    if document_type == "BOLETO":
        candidates = re.findall(r"(?:\d[\s.]*){44,48}", normalized)
        if candidates:
            barcode = max((_digits(c) for c in candidates), key=len, default="")[:48]

    return {
        "supplier": supplier,
        "supplier_document": _digits(cnpj_match.group(1)) if cnpj_match else "",
        "document_number": number_match.group(1).strip() if number_match else "",
        "issue_date": _parse_date(issue_match.group(1)) if issue_match else None,
        "due_date": _parse_date(due_match.group(1)) if due_match else None,
        "total_value": total,
        "barcode": barcode,
    }


@app.post("/api/import/pdf")
async def import_pdf(
    company_id: int = Form(...),
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    document_type = document_type.upper().strip()
    if document_type not in {"NOTA", "BOLETO"}:
        raise HTTPException(400, "Tipo deve ser NOTA ou BOLETO")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Selecione um arquivo PDF")

    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    target = UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    target.write_bytes(await file.read())

    try:
        reader = PdfReader(str(target))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Não foi possível ler o PDF: {exc}")

    fields = extract_pdf_fields(text, document_type)
    row = ImportedPdf(
        tenant_id=user.tenant_id,
        company_id=company_id,
        document_type=document_type,
        filename=file.filename,
        stored_path=str(target),
        extracted_text=text[:100000],
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "document_type": row.document_type,
        "filename": row.filename,
        "supplier": row.supplier,
        "supplier_document": row.supplier_document,
        "document_number": row.document_number,
        "issue_date": row.issue_date,
        "due_date": row.due_date,
        "total_value": float(row.total_value),
        "barcode": row.barcode,
        "status": row.status,
    }


@app.get("/api/import/pdf")
def list_imported_pdfs(
    company_id: int,
    document_type: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = db.query(ImportedPdf).filter(
        ImportedPdf.tenant_id == user.tenant_id,
        ImportedPdf.company_id == company_id,
    )
    if document_type:
        query = query.filter(ImportedPdf.document_type == document_type.upper())
    rows = query.order_by(ImportedPdf.id.desc()).all()
    return [
        {
            "id": r.id,
            "document_type": r.document_type,
            "filename": r.filename,
            "supplier": r.supplier,
            "document_number": r.document_number,
            "due_date": r.due_date,
            "total_value": float(r.total_value),
            "barcode": r.barcode,
            "status": r.status,
        }
        for r in rows
    ]


@app.post("/api/import/pdf/{document_id}/confirm")
def confirm_imported_pdf(
    document_id: int,
    data: PdfConfirmIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(ImportedPdf).filter(
        ImportedPdf.id == document_id,
        ImportedPdf.tenant_id == user.tenant_id,
    ).first()
    if not row:
        raise HTTPException(404, "Documento não encontrado")

    for field, value in data.model_dump().items():
        if field != "create_payable":
            setattr(row, field, value)

    if data.create_payable and data.total_value > 0:
        db.add(Payable(
            tenant_id=user.tenant_id,
            company_id=row.company_id,
            supplier=data.supplier or "Fornecedor não identificado",
            due_date=data.due_date or data.issue_date or date.today(),
            value=data.total_value,
            status="EM ABERTO",
        ))

    row.status = "CONFIRMADO"
    db.commit()
    return {"ok": True, "status": row.status}


MASTER_KEY_FILE = Path(__file__).resolve().parent.parent / "secure" / "master.key"
CERT_DIR = Path(__file__).resolve().parent.parent / "secure" / "certificates"
CERT_DIR.mkdir(parents=True, exist_ok=True)


def _master_fernet():
    if not MASTER_KEY_FILE.exists():
        MASTER_KEY_FILE.write_bytes(Fernet.generate_key())
    return Fernet(MASTER_KEY_FILE.read_bytes())


def _encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _master_fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _master_fernet().decrypt(value.encode("utf-8")).decode("utf-8")


@app.get("/api/fiscal/config")
def get_fiscal_config(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not row:
        return {
            "provider": "NUVEM_FISCAL",
            "environment": "HOMOLOGACAO",
            "automatic_issue": False,
            "series": "1",
            "last_number": 0,
            "has_client_id": False,
            "has_client_secret": False,
            "has_certificate": False,
        }
    return {
        "provider": row.provider,
        "environment": row.environment,
        "automatic_issue": row.automatic_issue,
        "series": row.series,
        "last_number": row.last_number,
        "has_client_id": bool(row.client_id_encrypted),
        "has_client_secret": bool(row.client_secret_encrypted),
        "has_certificate": bool(row.certificate_path),
    }


@app.post("/api/fiscal/config")
def save_fiscal_config(
    company_id: int,
    data: FiscalSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    row = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not row:
        row = FiscalConfig(
            tenant_id=user.tenant_id,
            company_id=company_id,
        )
        db.add(row)

    row.provider = data.provider
    row.environment = data.environment
    row.automatic_issue = data.automatic_issue
    row.series = data.series
    row.last_number = data.last_number
    if data.client_id:
        row.client_id_encrypted = _encrypt_secret(data.client_id)
    if data.client_secret:
        row.client_secret_encrypted = _encrypt_secret(data.client_secret)
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/fiscal/certificate")
async def upload_fiscal_certificate(
    company_id: int = Form(...),
    password: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    if not file.filename or not file.filename.lower().endswith((".pfx", ".p12")):
        raise HTTPException(400, "Envie um certificado .pfx ou .p12")

    row = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not row:
        row = FiscalConfig(tenant_id=user.tenant_id, company_id=company_id)
        db.add(row)
        db.flush()

    target = CERT_DIR / f"{user.tenant_id}_{company_id}_{uuid.uuid4().hex}.p12"
    target.write_bytes(await file.read())

    try:
        from app.services.sefaz_distribution import load_certificate_info
        info = load_certificate_info(str(target), password)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Certificado A1 inválido ou senha incorreta: {exc}")

    if info.valid_until < datetime.utcnow():
        target.unlink(missing_ok=True)
        raise HTTPException(400, "O certificado A1 está vencido")

    old_path = Path(row.certificate_path) if row.certificate_path else None
    row.certificate_path = str(target)
    row.certificate_password_encrypted = _encrypt_secret(password)
    row.updated_at = datetime.utcnow()
    db.commit()

    if old_path and old_path != target:
        old_path.unlink(missing_ok=True)

    return {
        "ok": True,
        "filename": file.filename,
        "subject": info.subject,
        "issuer": info.issuer,
        "valid_until": info.valid_until,
    }


@app.post("/api/companies/{company_id}/fill-from-certificate")
async def fill_company_from_certificate(
    company_id: int,
    password: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")
    if not file.filename or not file.filename.lower().endswith((".pfx", ".p12")):
        raise HTTPException(400, "Envie um certificado A1 .pfx ou .p12")

    target = CERT_DIR / f"{user.tenant_id}_{company_id}_{uuid.uuid4().hex}.p12"
    target.write_bytes(await file.read())

    try:
        certificate = extract_certificate_company(str(target), password)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Certificado A1 inválido ou senha incorreta: {exc}")

    if certificate.valid_until < datetime.utcnow():
        target.unlink(missing_ok=True)
        raise HTTPException(400, "O certificado A1 está vencido")

    current_cnpj = re.sub(r"\D", "", company.cnpj or "")
    if current_cnpj and current_cnpj != certificate.cnpj:
        target.unlink(missing_ok=True)
        raise HTTPException(
            409,
            "O certificado pertence a outro CNPJ. Selecione a empresa correta antes de continuar.",
        )

    warning = ""
    try:
        registry = fetch_company_registry(certificate.cnpj)
    except Exception as exc:
        registry = {
            "cnpj": certificate.cnpj,
            "legal_name": certificate.legal_name,
            "trade_name": certificate.legal_name,
            "state_registration": "",
            "registry_source": "Certificado A1",
            "registry_status": "",
        }
        warning = str(exc)

    expected_cnpj = re.sub(r"\D", "", registry.get("cnpj") or certificate.cnpj)
    if expected_cnpj and expected_cnpj != certificate.cnpj:
        target.unlink(missing_ok=True)
        raise HTTPException(502, "A consulta cadastral retornou um CNPJ diferente do certificado")

    fields = (
        "trade_name",
        "legal_name",
        "cnpj",
        "state_registration",
        "email",
        "phone",
        "address",
        "number",
        "complement",
        "district",
        "city",
        "state",
        "zip_code",
    )
    registry["cnpj"] = certificate.cnpj
    registry["legal_name"] = registry.get("legal_name") or certificate.legal_name
    registry["trade_name"] = registry.get("trade_name") or registry["legal_name"]
    for field in fields:
        value = str(registry.get(field) or "").strip()
        if value:
            setattr(company, field, value)

    fiscal = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not fiscal:
        fiscal = FiscalConfig(tenant_id=user.tenant_id, company_id=company_id)
        db.add(fiscal)
        db.flush()

    old_path = Path(fiscal.certificate_path) if fiscal.certificate_path else None
    fiscal.certificate_path = str(target)
    fiscal.certificate_password_encrypted = _encrypt_secret(password)
    fiscal.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(company)

    if old_path and old_path != target:
        old_path.unlink(missing_ok=True)

    company_payload = {
        field: getattr(company, field)
        for field in fields
    }
    return {
        "ok": True,
        "company": company_payload,
        "certificate": {
            "cnpj": certificate.cnpj,
            "subject": certificate.subject,
            "issuer": certificate.issuer,
            "serial_number": certificate.serial_number,
            "valid_from": certificate.valid_from,
            "valid_until": certificate.valid_until,
        },
        "registry_source": registry.get("registry_source", ""),
        "registry_status": registry.get("registry_status", ""),
        "state_registration_found": bool(company.state_registration),
        "warning": warning,
    }


@app.get("/api/fiscal/readiness")
def fiscal_readiness(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")
    config = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()

    company_ok = all([
        company.cnpj,
        company.state_registration,
        company.legal_name,
        company.address,
        company.city,
        company.state,
    ])
    provider_ok = bool(config and config.client_id_encrypted and config.client_secret_encrypted)
    certificate_ok = bool(config and config.certificate_path)
    return {
        "company_ok": company_ok,
        "provider_ok": provider_ok,
        "certificate_ok": certificate_ok,
        "ready_for_homologation": company_ok and provider_ok and certificate_ok,
        "production_enabled": bool(
            company_ok and provider_ok and certificate_ok
            and config and config.environment == "PRODUCAO"
        ),
    }


def _xml_text(node, path: str, ns: dict, default: str = "") -> str:
    found = node.find(path, ns)
    return (found.text or "").strip() if found is not None else default


def _xml_decimal(value: str) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", "."))
    except Exception:
        return Decimal("0")


def _xml_date(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except Exception:
            return None


NFE_XML_DIR = Path(__file__).resolve().parent.parent / "uploads" / "xml"
NFE_XML_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/customers")
def list_customers(
    company_id: int,
    search: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = db.query(Customer).filter(
        Customer.tenant_id == user.tenant_id,
        Customer.company_id == company_id,
        Customer.active.is_(True),
    )
    if search:
        term = f"%{search}%"
        query = query.filter(
            Customer.name.ilike(term) |
            Customer.trade_name.ilike(term) |
            Customer.document.ilike(term)
        )
    return query.order_by(Customer.name).all()


@app.post("/api/customers")
def create_customer(
    data: CustomerIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    person_type = data.person_type.upper()
    if person_type not in {"PF", "PJ"}:
        raise HTTPException(400, "Tipo deve ser PF ou PJ")
    document = re.sub(r"\D", "", data.document)
    if document:
        expected = 11 if person_type == "PF" else 14
        if len(document) != expected:
            raise HTTPException(400, f"Documento inválido para {person_type}")
    row = Customer(
        tenant_id=user.tenant_id,
        **data.model_dump(exclude={"document"}),
        document=document,
        person_type=person_type,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/nfe/import")
async def import_nfe_xml(
    company_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not file.filename or not file.filename.lower().endswith(".xml"):
        raise HTTPException(400, "Selecione um XML de NF-e")

    raw = await file.read()
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        raise HTTPException(400, f"XML inválido: {exc}")

    ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
    inf_nfe = root.find(".//nfe:infNFe", ns)
    if inf_nfe is None:
        raise HTTPException(400, "O arquivo não parece ser uma NF-e válida")

    access_key = (inf_nfe.attrib.get("Id") or "").replace("NFe", "")
    if len(access_key) != 44:
        access_key = _xml_text(root, ".//nfe:protNFe/nfe:infProt/nfe:chNFe", ns)

    duplicate = db.query(ImportedNfe).filter(
        ImportedNfe.tenant_id == user.tenant_id,
        ImportedNfe.company_id == company_id,
        ImportedNfe.access_key == access_key,
    ).first()
    if duplicate:
        raise HTTPException(409, "Esta NF-e já foi importada")

    ide = inf_nfe.find("nfe:ide", ns)
    emit = inf_nfe.find("nfe:emit", ns)
    total_node = inf_nfe.find("nfe:total/nfe:ICMSTot", ns)

    supplier_name = _xml_text(emit, "nfe:xNome", ns) if emit is not None else ""
    supplier_cnpj = _xml_text(emit, "nfe:CNPJ", ns) if emit is not None else ""
    invoice_number = _xml_text(ide, "nfe:nNF", ns) if ide is not None else ""
    series = _xml_text(ide, "nfe:serie", ns) if ide is not None else ""
    issue_date = _xml_date(_xml_text(ide, "nfe:dhEmi", ns) if ide is not None else "")
    total_value = _xml_decimal(_xml_text(total_node, "nfe:vNF", ns) if total_node is not None else "0")

    target = NFE_XML_DIR / f"{uuid.uuid4().hex}.xml"
    target.write_bytes(raw)

    supplier = db.query(Supplier).filter(
        Supplier.tenant_id == user.tenant_id,
        Supplier.company_id == company_id,
        Supplier.cnpj == supplier_cnpj,
    ).first()
    if not supplier:
        supplier = Supplier(
            tenant_id=user.tenant_id,
            company_id=company_id,
            legal_name=supplier_name or "Fornecedor importado",
            cnpj=supplier_cnpj,
            state_registration=_xml_text(emit, "nfe:IE", ns) if emit is not None else "",
        )
        db.add(supplier)

    nfe = ImportedNfe(
        tenant_id=user.tenant_id,
        company_id=company_id,
        access_key=access_key,
        invoice_number=invoice_number,
        series=series,
        issue_date=issue_date,
        supplier_name=supplier_name,
        supplier_cnpj=supplier_cnpj,
        total_value=total_value,
        filename=file.filename,
        stored_path=str(target),
        status="PENDENTE",
    )
    db.add(nfe)
    db.flush()

    response_items = []
    for det in inf_nfe.findall("nfe:det", ns):
        prod = det.find("nfe:prod", ns)
        if prod is None:
            continue
        code = _xml_text(prod, "nfe:cProd", ns)
        barcode = _xml_text(prod, "nfe:cEAN", ns)
        if barcode == "SEM GTIN":
            barcode = ""
        item = ImportedNfeItem(
            nfe_id=nfe.id,
            product_code=code,
            barcode=barcode,
            description=_xml_text(prod, "nfe:xProd", ns),
            ncm=_xml_text(prod, "nfe:NCM", ns),
            cfop=_xml_text(prod, "nfe:CFOP", ns),
            unit=_xml_text(prod, "nfe:uCom", ns, "UN"),
            invoiced_quantity=_xml_decimal(_xml_text(prod, "nfe:qCom", ns)),
            received_quantity=_xml_decimal(_xml_text(prod, "nfe:qCom", ns)),
            unit_value=_xml_decimal(_xml_text(prod, "nfe:vUnCom", ns)),
            total_value=_xml_decimal(_xml_text(prod, "nfe:vProd", ns)),
        )

        match = None
        if barcode:
            match = db.query(Product).filter(
                Product.tenant_id == user.tenant_id,
                Product.company_id == company_id,
                Product.barcode == barcode,
            ).first()
        if not match and code:
            match = db.query(Product).filter(
                Product.tenant_id == user.tenant_id,
                Product.company_id == company_id,
                Product.sku == code,
            ).first()
        if match:
            item.matched_product_id = match.id

        db.add(item)
        db.flush()
        response_items.append({
            "id": item.id,
            "product_code": item.product_code,
            "barcode": item.barcode,
            "description": item.description,
            "ncm": item.ncm,
            "cfop": item.cfop,
            "unit": item.unit,
            "invoiced_quantity": float(item.invoiced_quantity),
            "received_quantity": float(item.received_quantity),
            "unit_value": float(item.unit_value),
            "total_value": float(item.total_value),
            "matched_product_id": item.matched_product_id,
        })

    installments = []
    for dup in inf_nfe.findall(".//nfe:cobr/nfe:dup", ns):
        inst = NfeInstallment(
            nfe_id=nfe.id,
            installment_number=_xml_text(dup, "nfe:nDup", ns),
            due_date=_xml_date(_xml_text(dup, "nfe:dVenc", ns)),
            value=_xml_decimal(_xml_text(dup, "nfe:vDup", ns)),
        )
        db.add(inst)
        db.flush()
        installments.append({
            "id": inst.id,
            "installment_number": inst.installment_number,
            "due_date": inst.due_date,
            "value": float(inst.value),
        })

    db.commit()
    return {
        "id": nfe.id,
        "access_key": nfe.access_key,
        "invoice_number": nfe.invoice_number,
        "series": nfe.series,
        "issue_date": nfe.issue_date,
        "supplier_name": nfe.supplier_name,
        "supplier_cnpj": nfe.supplier_cnpj,
        "total_value": float(nfe.total_value),
        "items": response_items,
        "installments": installments,
    }


@app.post("/api/nfe/{nfe_id}/confirm")
def confirm_nfe(
    nfe_id: int,
    data: ConfirmNfeIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    nfe = db.query(ImportedNfe).filter(
        ImportedNfe.id == nfe_id,
        ImportedNfe.tenant_id == user.tenant_id,
    ).first()
    if not nfe:
        raise HTTPException(404, "NF-e não encontrada")
    if nfe.status == "CONFIRMADA":
        raise HTTPException(409, "Esta NF-e já foi confirmada")

    item_map = {row.item_id: row for row in data.items}
    imported_items = db.query(ImportedNfeItem).filter(ImportedNfeItem.nfe_id == nfe.id).all()

    created_products = 0
    movements = 0
    for item in imported_items:
        choice = item_map.get(item.id)
        if not choice:
            continue
        qty = choice.received_quantity
        if qty <= 0:
            continue

        product = None
        if choice.product_id:
            product = db.query(Product).filter(
                Product.id == choice.product_id,
                Product.tenant_id == user.tenant_id,
                Product.company_id == nfe.company_id,
            ).first()
        elif item.matched_product_id:
            product = db.get(Product, item.matched_product_id)

        if not product and choice.create_product:
            sku = item.product_code or f"NF{nfe.id}-{item.id}"
            product = Product(
                tenant_id=user.tenant_id,
                company_id=nfe.company_id,
                sku=sku,
                barcode=item.barcode,
                name=item.description,
                category="Importado da NF-e",
                unit=item.unit or "UN",
                unit_cost=item.unit_value,
                sale_price=Decimal("0"),
                minimum_stock=0,
            )
            db.add(product)
            db.flush()
            created_products += 1

        if not product:
            continue

        item.matched_product_id = product.id
        item.received_quantity = qty
        product.unit_cost = item.unit_value

        db.add(StockMovement(
            tenant_id=user.tenant_id,
            company_id=nfe.company_id,
            product_id=product.id,
            movement_type="ENTRADA",
            quantity=int(qty),
            unit_value=item.unit_value,
            document=f"NFE-{nfe.access_key}",
            movement_date=nfe.issue_date or date.today(),
        ))
        movements += 1

    payables_created = 0
    if data.create_payables:
        installments = db.query(NfeInstallment).filter(NfeInstallment.nfe_id == nfe.id).all()
        if installments:
            for inst in installments:
                db.add(Payable(
                    tenant_id=user.tenant_id,
                    company_id=nfe.company_id,
                    supplier=nfe.supplier_name,
                    due_date=inst.due_date or nfe.issue_date or date.today(),
                    value=inst.value,
                    status="EM ABERTO",
                ))
                payables_created += 1
        elif nfe.total_value > 0:
            db.add(Payable(
                tenant_id=user.tenant_id,
                company_id=nfe.company_id,
                supplier=nfe.supplier_name,
                due_date=nfe.issue_date or date.today(),
                value=nfe.total_value,
                status="EM ABERTO",
            ))
            payables_created = 1

    nfe.status = "CONFIRMADA"
    db.commit()
    return {
        "ok": True,
        "products_created": created_products,
        "stock_movements": movements,
        "payables_created": payables_created,
    }


@app.get("/api/payables/due-summary")
def payables_due_summary(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    today = date.today()
    rows = db.query(Payable).filter(
        Payable.tenant_id == user.tenant_id,
        Payable.company_id == company_id,
        Payable.status != "PAGO",
    ).order_by(Payable.due_date).all()

    result = {"overdue": [], "today": [], "next_7_days": [], "next_30_days": []}
    totals = {key: Decimal("0") for key in result}
    for row in rows:
        delta = (row.due_date - today).days
        item = {
            "id": row.id,
            "supplier": row.supplier,
            "due_date": row.due_date,
            "value": float(row.value),
            "status": "VENCIDO" if delta < 0 else "EM ABERTO",
        }
        if delta < 0:
            key = "overdue"
        elif delta == 0:
            key = "today"
        elif delta <= 7:
            key = "next_7_days"
        elif delta <= 30:
            key = "next_30_days"
        else:
            continue
        result[key].append(item)
        totals[key] += row.value

    return {
        **result,
        "totals": {key: float(value) for key, value in totals.items()},
    }


@app.get("/api/payables/monthly-summary")
def monthly_payables_summary(
    company_id: int,
    year: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    months = []
    for month in range(1, 13):
        total = db.query(func.coalesce(func.sum(Payable.value), 0)).filter(
            Payable.tenant_id == user.tenant_id,
            Payable.company_id == company_id,
            func.extract("year", Payable.due_date) == year,
            func.extract("month", Payable.due_date) == month,
            Payable.status != "PAGO",
        ).scalar()
        paid = db.query(func.coalesce(func.sum(Payable.value), 0)).filter(
            Payable.tenant_id == user.tenant_id,
            Payable.company_id == company_id,
            func.extract("year", Payable.due_date) == year,
            func.extract("month", Payable.due_date) == month,
            Payable.status == "PAGO",
        ).scalar()
        months.append({
            "month": month,
            "open": float(total or 0),
            "paid": float(paid or 0),
        })
    return months


@app.get("/api/fiscal/documents")
def list_fiscal_documents(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(FiscalDocument).filter(
        FiscalDocument.tenant_id == user.tenant_id,
        FiscalDocument.company_id == company_id,
    ).order_by(FiscalDocument.id.desc()).all()
    return [
        {
            "id": row.id,
            "sale_id": row.sale_id,
            "document_type": row.document_type,
            "environment": row.environment,
            "status": row.status,
            "access_key": row.access_key,
            "protocol": row.protocol,
            "error_message": row.error_message,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@app.get("/api/gmail/status")
def gmail_status(db: Session = Depends(get_db), user: User = Depends(current_user)):
    company_ids = [row.id for row in db.query(Company).filter(Company.tenant_id == user.tenant_id).all()]
    logs = db.query(GmailImportLog).filter(
        (GmailImportLog.tenant_id == user.tenant_id) |
        (GmailImportLog.company_id.in_(company_ids) if company_ids else False)
    ).order_by(GmailImportLog.processed_at.desc()).limit(50).all()
    return {
        "configured": gmail_is_configured(),
        "automatic_interval": "5 minutos (quando o Cron estiver ativo)",
        "logs": [{
            "id": row.id,
            "sender": row.sender,
            "subject": row.subject,
            "filename": row.filename,
            "access_key": row.access_key,
            "status": row.status,
            "detail": row.detail,
            "processed_at": row.processed_at,
        } for row in logs],
    }


@app.post("/api/gmail/sync")
def gmail_sync(user: User = Depends(current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Somente administradores podem executar a importação.")
    if not gmail_is_configured():
        raise HTTPException(400, "Gmail ainda não configurado no Railway.")
    try:
        return sync_once()
    except Exception as exc:
        raise HTTPException(500, str(exc))



@app.get("/api/stock/intelligence")
def stock_intelligence(
    company_id: int,
    period_days: int = 90,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if period_days not in {30, 60, 90, 180}:
        period_days = 90

    today = date.today()
    since = today - timedelta(days=period_days - 1)
    products = db.query(Product).filter(
        Product.tenant_id == user.tenant_id,
        Product.company_id == company_id,
    ).order_by(Product.name).all()

    sales_rows = db.query(
        SaleItem.product_id,
        func.coalesce(func.sum(SaleItem.quantity), 0).label("sold_quantity"),
        func.coalesce(func.sum(SaleItem.total), 0).label("sold_value"),
        func.max(Sale.sale_date).label("last_sale_date"),
    ).join(Sale, Sale.id == SaleItem.sale_id).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= since,
        Sale.sale_date <= today,
        Sale.status == "CONCLUÍDA",
    ).group_by(SaleItem.product_id).all()

    sales_by_product = {
        int(row.product_id): {
            "quantity": int(row.sold_quantity or 0),
            "value": float(row.sold_value or 0),
            "last_sale_date": row.last_sale_date,
        }
        for row in sales_rows
    }

    total_revenue = sum(item["value"] for item in sales_by_product.values())
    revenue_order = sorted(
        ((product.id, sales_by_product.get(product.id, {}).get("value", 0.0)) for product in products),
        key=lambda item: item[1],
        reverse=True,
    )
    abc_by_product = {}
    accumulated = 0.0
    for product_id, revenue in revenue_order:
        accumulated += revenue
        share = (accumulated / total_revenue) if total_revenue > 0 else 1.0
        abc_by_product[product_id] = "A" if share <= 0.80 else ("B" if share <= 0.95 else "C")

    rows = []
    summary = {
        "products": len(products),
        "stock_units": 0,
        "stock_value": 0.0,
        "low_stock": 0,
        "zero_stock": 0,
        "negative_stock": 0,
        "without_sales": 0,
        "suggested_purchase_units": 0,
        "suggested_purchase_value": 0.0,
    }

    for product in products:
        current_stock = stock_of(db, user.tenant_id, company_id, product.id)
        sales = sales_by_product.get(product.id, {})
        sold_quantity = int(sales.get("quantity", 0))
        sold_value = float(sales.get("value", 0))
        last_sale_date = sales.get("last_sale_date")
        average_daily = sold_quantity / period_days
        coverage_days = round(current_stock / average_daily, 1) if average_daily > 0 and current_stock > 0 else None
        demand_30_days = math.ceil(average_daily * 30)
        target_stock = max(int(product.minimum_stock or 0), demand_30_days)
        suggested_quantity = max(target_stock - current_stock, 0)
        days_without_sale = (today - last_sale_date).days if last_sale_date else None

        if current_stock < 0:
            status = "NEGATIVO"
        elif current_stock == 0:
            status = "ZERADO"
        elif current_stock <= int(product.minimum_stock or 0):
            status = "BAIXO"
        elif sold_quantity == 0:
            status = "SEM_VENDA"
        else:
            status = "NORMAL"

        unit_cost = float(product.unit_cost or 0)
        inventory_value = max(current_stock, 0) * unit_cost
        suggested_value = suggested_quantity * unit_cost

        summary["stock_units"] += current_stock
        summary["stock_value"] += inventory_value
        summary["suggested_purchase_units"] += suggested_quantity
        summary["suggested_purchase_value"] += suggested_value
        if current_stock <= int(product.minimum_stock or 0):
            summary["low_stock"] += 1
        if current_stock == 0:
            summary["zero_stock"] += 1
        if current_stock < 0:
            summary["negative_stock"] += 1
        if sold_quantity == 0:
            summary["without_sales"] += 1

        rows.append({
            "id": product.id,
            "name": product.name,
            "sku": product.sku,
            "barcode": product.barcode,
            "category": product.category,
            "unit": product.unit,
            "current_stock": current_stock,
            "minimum_stock": int(product.minimum_stock or 0),
            "unit_cost": unit_cost,
            "sale_price": float(product.sale_price or 0),
            "inventory_value": round(inventory_value, 2),
            "sold_quantity": sold_quantity,
            "sold_value": round(sold_value, 2),
            "average_daily_sales": round(average_daily, 3),
            "coverage_days": coverage_days,
            "days_without_sale": days_without_sale,
            "abc_class": abc_by_product.get(product.id, "C"),
            "suggested_quantity": suggested_quantity,
            "suggested_value": round(suggested_value, 2),
            "status": status,
        })

    summary["stock_value"] = round(summary["stock_value"], 2)
    summary["suggested_purchase_value"] = round(summary["suggested_purchase_value"], 2)
    rows.sort(key=lambda row: (
        {"NEGATIVO": 0, "ZERADO": 1, "BAIXO": 2, "SEM_VENDA": 3, "NORMAL": 4}.get(row["status"], 5),
        row["name"].lower(),
    ))
    return {
        "period_days": period_days,
        "generated_at": datetime.utcnow(),
        "summary": summary,
        "items": rows,
    }


@app.put("/api/products/{product_id}/minimum-stock")
def update_product_minimum_stock(
    product_id: int,
    company_id: int,
    data: dict,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.tenant_id == user.tenant_id,
        Product.company_id == company_id,
    ).first()
    if not product:
        raise HTTPException(404, "Produto não encontrado")
    try:
        minimum_stock = int(data.get("minimum_stock", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Estoque mínimo inválido")
    if minimum_stock < 0:
        raise HTTPException(400, "O estoque mínimo não pode ser negativo")
    product.minimum_stock = minimum_stock
    db.commit()
    return {"id": product.id, "minimum_stock": product.minimum_stock}

@app.get("/api/dashboard")
def dashboard(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    today = date.today()
    month_start = today.replace(day=1)

    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    products = db.query(Product).filter(
        Product.tenant_id == user.tenant_id,
        Product.company_id == company_id,
    ).all()

    stock_units = 0
    stock_value = 0.0
    low_stock_items = []
    for product in products:
        stock = stock_of(db, user.tenant_id, company_id, product.id)
        stock_units += stock
        stock_value += stock * float(product.unit_cost or 0)
        minimum = int(product.minimum_stock or 0)
        if minimum > 0 and stock <= minimum:
            low_stock_items.append({
                "id": product.id,
                "name": product.name,
                "stock": stock,
                "minimum_stock": minimum,
            })

    sales_month_total, sales_month_count = db.query(
        func.coalesce(func.sum(Sale.total), 0),
        func.count(Sale.id),
    ).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= month_start,
        Sale.sale_date <= today,
        Sale.status == "CONCLUÍDA",
    ).one()

    sales_today_total, sales_today_count = db.query(
        func.coalesce(func.sum(Sale.total), 0),
        func.count(Sale.id),
    ).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date == today,
        Sale.status == "CONCLUÍDA",
    ).one()

    payables_rows = db.query(Payable).filter(
        Payable.tenant_id == user.tenant_id,
        Payable.company_id == company_id,
        Payable.status != "PAGO",
    ).all()
    payables_open = sum((Decimal(str(row.value or 0)) for row in payables_rows), Decimal("0"))
    overdue_payables = [row for row in payables_rows if row.due_date and row.due_date < today]
    next_7_payables = [
        row for row in payables_rows
        if row.due_date and 0 <= (row.due_date - today).days <= 7
    ]

    receivable_rows = db.query(Receivable).filter(
        Receivable.tenant_id == user.tenant_id,
        Receivable.company_id == company_id,
        Receivable.status == "EM ABERTO",
    ).all()
    receivables_open = sum(
        (Decimal(str(row.value or 0)) for row in receivable_rows),
        Decimal("0"),
    )
    overdue_receivables = [
        row for row in receivable_rows
        if row.due_date and row.due_date < today
    ]

    received_month = db.query(
        func.coalesce(func.sum(Receivable.value), 0)
    ).filter(
        Receivable.tenant_id == user.tenant_id,
        Receivable.company_id == company_id,
        Receivable.status == "RECEBIDO",
        Receivable.received_date >= month_start,
        Receivable.received_date <= today,
    ).scalar() or 0

    sales_chart = []
    cursor_year = today.year
    cursor_month = today.month
    months = []
    for _ in range(6):
        months.append((cursor_year, cursor_month))
        cursor_month -= 1
        if cursor_month == 0:
            cursor_month = 12
            cursor_year -= 1
    months.reverse()

    month_labels = [
        "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
        "Jul", "Ago", "Set", "Out", "Nov", "Dez",
    ]
    for year_value, month_value in months:
        if month_value == 12:
            next_year, next_month = year_value + 1, 1
        else:
            next_year, next_month = year_value, month_value + 1
        start = date(year_value, month_value, 1)
        end = date(next_year, next_month, 1)
        total = db.query(func.coalesce(func.sum(Sale.total), 0)).filter(
            Sale.tenant_id == user.tenant_id,
            Sale.company_id == company_id,
            Sale.sale_date >= start,
            Sale.sale_date < end,
            Sale.status == "CONCLUÍDA",
        ).scalar() or 0
        sales_chart.append({
            "label": month_labels[month_value - 1],
            "value": float(total),
        })

    top_products_rows = db.query(
        Product.name,
        func.coalesce(func.sum(SaleItem.quantity), 0).label("quantity"),
        func.coalesce(func.sum(SaleItem.total), 0).label("value"),
    ).join(
        SaleItem, SaleItem.product_id == Product.id,
    ).join(
        Sale, Sale.id == SaleItem.sale_id,
    ).filter(
        Product.tenant_id == user.tenant_id,
        Product.company_id == company_id,
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= month_start,
        Sale.sale_date <= today,
        Sale.status == "CONCLUÍDA",
    ).group_by(
        Product.id,
        Product.name,
    ).order_by(
        func.sum(SaleItem.quantity).desc(),
    ).limit(5).all()

    recent_sales_rows = db.query(Sale).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
    ).order_by(
        Sale.sale_date.desc(),
        Sale.id.desc(),
    ).limit(6).all()

    # Integrações: todas protegidas para que um módulo opcional nunca derrube o Dashboard.
    sefaz_pending = 0
    try:
        from app.models import SefazDistributionDocument
        sefaz_pending = db.query(func.count(SefazDistributionDocument.id)).filter(
            SefazDistributionDocument.tenant_id == user.tenant_id,
            SefazDistributionDocument.company_id == company_id,
            SefazDistributionDocument.status.in_([
                "RECEBIDO",
                "AGUARDANDO_MANIFESTACAO",
                "MANIFESTADO_AGUARDANDO_XML",
            ]),
        ).scalar() or 0
    except Exception:
        db.rollback()

    dda_connected = False
    dda_open_count = 0
    dda_open_value = Decimal("0")
    try:
        dda_connected = db.query(DdaConnector).filter(
            DdaConnector.tenant_id == user.tenant_id,
            DdaConnector.company_id == company_id,
            DdaConnector.active.is_(True),
        ).count() > 0
        dda_rows = db.query(DdaBoleto).filter(
            DdaBoleto.tenant_id == user.tenant_id,
            DdaBoleto.company_id == company_id,
            ~DdaBoleto.status.in_(["PAGO", "CANCELADO", "IGNORADO"]),
        ).all()
        dda_open_count = len(dda_rows)
        dda_open_value = sum(
            (Decimal(str(row.amount or 0)) for row in dda_rows),
            Decimal("0"),
        )
    except Exception:
        db.rollback()

    pagbank_connected = False
    pagbank_pending_count = 0
    try:
        pagbank_connected = db.query(PagBankConfig).filter(
            PagBankConfig.tenant_id == user.tenant_id,
            PagBankConfig.company_id == company_id,
            PagBankConfig.active.is_(True),
        ).count() > 0
        paid_statuses = ["PAID", "AUTHORIZED", "AVAILABLE", "IN_ANALYSIS_APPROVED"]
        pagbank_pending_count = db.query(PagBankPayment).filter(
            PagBankPayment.tenant_id == user.tenant_id,
            PagBankPayment.company_id == company_id,
            ~PagBankPayment.status.in_(paid_statuses),
        ).count()
    except Exception:
        db.rollback()

    shopee_connected_shops = 0
    try:
        shopee_connected_shops = db.query(ShopeeShop).filter(
            ShopeeShop.tenant_id == user.tenant_id,
            ShopeeShop.company_id == company_id,
            ShopeeShop.connected.is_(True),
        ).count()
    except Exception:
        db.rollback()

    bank_pending_count = 0
    try:
        bank_pending_count = db.query(BankTransaction).filter(
            BankTransaction.tenant_id == user.tenant_id,
            BankTransaction.company_id == company_id,
            BankTransaction.reconciliation_status == "PENDENTE",
        ).count()
    except Exception:
        db.rollback()

    return {
        "company_name": company.trade_name or company.legal_name or "Empresa",
        "has_operation": bool(
            products
            or int(sales_month_count or 0)
            or payables_rows
            or receivable_rows
        ),

        "products": len(products),
        "stock_units": int(stock_units),
        "stock_value": float(stock_value),
        "low_stock": len(low_stock_items),
        "low_stock_items": low_stock_items[:8],

        "sales_today": float(sales_today_total or 0),
        "sales_today_count": int(sales_today_count or 0),
        "sales_month": float(sales_month_total or 0),
        "sales_month_count": int(sales_month_count or 0),
        "sales_chart": sales_chart,

        "payables_open": float(payables_open),
        "payables_count": len(payables_rows),
        "overdue_payables_count": len(overdue_payables),
        "overdue_payables_value": float(sum(
            (Decimal(str(row.value or 0)) for row in overdue_payables),
            Decimal("0"),
        )),
        "payables_next_7_days_count": len(next_7_payables),
        "payables_next_7_days_value": float(sum(
            (Decimal(str(row.value or 0)) for row in next_7_payables),
            Decimal("0"),
        )),

        "receivables_open": float(receivables_open),
        "receivables_count": len(receivable_rows),
        "overdue_receivables_count": len(overdue_receivables),
        "overdue_receivables_value": float(sum(
            (Decimal(str(row.value or 0)) for row in overdue_receivables),
            Decimal("0"),
        )),
        "received_month": float(received_month),
        "open_balance": float(receivables_open - payables_open),

        "sefaz_pending": int(sefaz_pending),
        "dda_connected": bool(dda_connected),
        "dda_open_count": int(dda_open_count),
        "dda_open_value": float(dda_open_value),
        "pagbank_connected": bool(pagbank_connected),
        "pagbank_pending_count": int(pagbank_pending_count),
        "shopee_connected": shopee_connected_shops > 0,
        "shopee_connected_shops": int(shopee_connected_shops),
        "bank_pending_count": int(bank_pending_count),

        "top_products": [
            {
                "name": row.name,
                "quantity": int(row.quantity or 0),
                "value": float(row.value or 0),
            }
            for row in top_products_rows
        ],
        "recent_sales": [
            {
                "id": row.id,
                "number": row.number,
                "customer": row.customer_name,
                "date": row.sale_date.isoformat(),
                "total": float(row.total or 0),
                "status": row.status,
            }
            for row in recent_sales_rows
        ],
    }

@app.get("/api/marketplaces/shopee/status")
def shopee_status(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    shops = db.query(ShopeeShop).filter(
        ShopeeShop.tenant_id == user.tenant_id,
        ShopeeShop.company_id == company_id,
    ).order_by(ShopeeShop.id.desc()).all()
    return {
        "configured": shopee_configured(),
        "approval_pending": not shopee_configured(),
        "shops": [
            {
                "id": row.id,
                "shop_id": row.shop_id,
                "shop_name": row.shop_name or f"Loja {row.shop_id}",
                "region": row.region,
                "connected": row.connected,
                "last_sync_at": row.last_sync_at.isoformat()
                if row.last_sync_at else None,
            }
            for row in shops
        ],
    }


@app.get("/api/marketplaces/shopee/connect-url")
def shopee_connect_url(
    company_id: int,
    user: User = Depends(current_user),
):
    if not shopee_configured():
        raise HTTPException(
            400,
            "Aguardando aprovação do aplicativo e configuração "
            "de SHOPEE_PARTNER_ID, SHOPEE_PARTNER_KEY e SHOPEE_REDIRECT_URL.",
        )
    state_payload = (
        f"{user.tenant_id}:{company_id}:{user.id}:"
        f"{int(datetime.utcnow().timestamp())}"
    )
    state = base64.urlsafe_b64encode(
        state_payload.encode("utf-8")
    ).decode("ascii")
    return {"url": shopee_authorization_url(state)}


@app.get("/api/marketplaces/shopee/callback")
def shopee_callback(
    code: str,
    shop_id: int,
    state: str,
    db: Session = Depends(get_db),
):
    try:
        decoded = base64.urlsafe_b64decode(
            state.encode("ascii")
        ).decode("utf-8")
        tenant_id, company_id, user_id, _ = [
            int(part) for part in decoded.split(":")
        ]
    except Exception as exc:
        raise HTTPException(400, "Estado OAuth inválido") from exc

    tokens = shopee_exchange_code(code, shop_id)
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expire_in = int(tokens.get("expire_in", 0) or 0)

    shop = db.query(ShopeeShop).filter(
        ShopeeShop.tenant_id == tenant_id,
        ShopeeShop.company_id == company_id,
        ShopeeShop.shop_id == shop_id,
    ).first()
    if not shop:
        shop = ShopeeShop(
            tenant_id=tenant_id,
            company_id=company_id,
            shop_id=shop_id,
        )
        db.add(shop)

    shop.access_token_encrypted = _encrypt_secret(access_token)
    shop.refresh_token_encrypted = _encrypt_secret(refresh_token)
    shop.token_expires_at = (
        datetime.utcnow() + timedelta(seconds=expire_in)
        if expire_in else None
    )
    shop.connected = True
    shop.updated_at = datetime.utcnow()
    db.commit()

    try:
        info = shopee_shop_get(
            "/api/v2/shop/get_shop_info",
            access_token,
            shop_id,
        )
        response = info.get("response") or {}
        shop.shop_name = response.get("shop_name", "") or shop.shop_name
        shop.region = response.get("region", "BR") or "BR"
        db.commit()
    except Exception:
        db.rollback()

    return {
        "ok": True,
        "message": "Loja Shopee conectada. Pode voltar ao Gestão Fácil.",
        "shop_id": shop_id,
    }


@app.post("/api/marketplaces/shopee/sync")
def shopee_sync_orders(
    company_id: int,
    shop_record_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    shop = db.query(ShopeeShop).filter(
        ShopeeShop.id == shop_record_id,
        ShopeeShop.tenant_id == user.tenant_id,
        ShopeeShop.company_id == company_id,
    ).first()
    if not shop:
        raise HTTPException(404, "Loja Shopee não encontrada")

    access_token = _decrypt_secret(shop.access_token_encrypted)
    refresh_token_value = _decrypt_secret(shop.refresh_token_encrypted)

    if (
        shop.token_expires_at
        and shop.token_expires_at <= datetime.utcnow() + timedelta(minutes=5)
    ):
        refreshed = shopee_refresh_token(
            refresh_token_value,
            shop.shop_id,
        )
        access_token = refreshed.get("access_token", access_token)
        refresh_token_value = refreshed.get(
            "refresh_token",
            refresh_token_value,
        )
        shop.access_token_encrypted = _encrypt_secret(access_token)
        shop.refresh_token_encrypted = _encrypt_secret(refresh_token_value)
        expire_in = int(refreshed.get("expire_in", 0) or 0)
        shop.token_expires_at = (
            datetime.utcnow() + timedelta(seconds=expire_in)
            if expire_in else None
        )
        db.commit()

    now = int(datetime.utcnow().timestamp())
    start = now - (15 * 24 * 60 * 60)
    result = shopee_shop_get(
        "/api/v2/order/get_order_list",
        access_token,
        shop.shop_id,
        time_range_field="create_time",
        time_from=start,
        time_to=now,
        page_size=50,
        response_optional_fields="order_status",
    )
    response = result.get("response") or {}
    order_list = response.get("order_list") or []

    imported = 0
    updated = 0
    for item in order_list:
        order_sn = str(item.get("order_sn") or "")
        if not order_sn:
            continue
        row = db.query(ShopeeOrder).filter(
            ShopeeOrder.order_sn == order_sn
        ).first()
        if not row:
            row = ShopeeOrder(
                tenant_id=user.tenant_id,
                company_id=company_id,
                shopee_shop_id=shop.id,
                order_sn=order_sn,
            )
            db.add(row)
            imported += 1
        else:
            updated += 1
        row.status = str(item.get("order_status") or "")
        row.raw_json = json.dumps(item, ensure_ascii=False)
        row.updated_at = datetime.utcnow()

    shop.last_sync_at = datetime.utcnow()
    db.add(ShopeeSyncLog(
        tenant_id=user.tenant_id,
        company_id=company_id,
        shopee_shop_id=shop.id,
        action="SYNC_ORDERS",
        status="OK",
        message=f"{imported} novo(s), {updated} atualizado(s).",
    ))
    db.commit()
    return {
        "ok": True,
        "imported": imported,
        "updated": updated,
        "total_found": len(order_list),
    }


@app.get("/api/marketplaces/shopee/orders")
def shopee_orders(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(ShopeeOrder).filter(
        ShopeeOrder.tenant_id == user.tenant_id,
        ShopeeOrder.company_id == company_id,
    ).order_by(ShopeeOrder.updated_at.desc()).limit(200).all()
    return [
        {
            "order_sn": row.order_sn,
            "status": row.status,
            "buyer_username": row.buyer_username,
            "total_amount": float(row.total_amount or 0),
            "tracking_number": row.tracking_number,
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]

class DdaConnectorIn(BaseModel):
    provider: str
    name: str = ""
    environment: str = "PRODUCAO"
    credentials: dict = {}
    active: bool = False
    auto_create_payable: bool = True
    require_invoice_match: bool = False


class DdaManualBoletoIn(BaseModel):
    external_id: str = ""
    digitable_line: str = ""
    beneficiary_name: str
    beneficiary_document: str = ""
    payer_document: str = ""
    issue_date: date | None = None
    due_date: date
    amount: Decimal
    bank_status: str = "EM_ABERTO"


def _dda_connector_payload(row: DdaConnector) -> dict:
    provider = DDA_SUPPORTED_PROVIDERS.get(row.provider, {})
    return {
        "id": row.id,
        "provider": row.provider,
        "provider_label": provider.get("label", row.provider),
        "name": row.name,
        "environment": row.environment,
        "active": row.active,
        "auto_create_payable": row.auto_create_payable,
        "require_invoice_match": row.require_invoice_match,
        "last_sync_at": row.last_sync_at,
        "last_status": row.last_status,
        "last_message": row.last_message,
        "has_credentials": bool(row.credentials_encrypted),
    }


def _dda_boleto_payload(row: DdaBoleto) -> dict:
    return {
        "id": row.id,
        "external_id": row.external_id,
        "digitable_line": row.digitable_line,
        "beneficiary_name": row.beneficiary_name,
        "beneficiary_document": row.beneficiary_document,
        "payer_document": row.payer_document,
        "issue_date": row.issue_date,
        "due_date": row.due_date,
        "amount": float(row.amount or 0),
        "status": row.status,
        "bank_status": row.bank_status,
        "invoice_id": row.invoice_id,
        "payable_id": row.payable_id,
        "match_score": row.match_score,
        "risk_level": row.risk_level,
        "recommendation": row.recommendation,
        "detected_at": row.detected_at,
    }


def _dda_find_matches(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    beneficiary_document: str,
    beneficiary_name: str,
    amount: Decimal,
):
    supplier = None
    if beneficiary_document:
        supplier = db.query(Supplier).filter(
            Supplier.tenant_id == tenant_id,
            Supplier.company_id == company_id,
            Supplier.cnpj == beneficiary_document,
        ).first()

    nfe_query = db.query(ImportedNfe).filter(
        ImportedNfe.tenant_id == tenant_id,
        ImportedNfe.company_id == company_id,
        ImportedNfe.total_value == amount,
    )
    if beneficiary_document:
        nfe_query = nfe_query.filter(
            ImportedNfe.supplier_cnpj == beneficiary_document
        )
    invoice = nfe_query.order_by(ImportedNfe.id.desc()).first()
    return supplier, invoice


def _dda_upsert(
    db: Session,
    *,
    user: User,
    company_id: int,
    connector: DdaConnector | None,
    normalized: dict,
) -> tuple[DdaBoleto, bool]:
    external_id = str(normalized.get("external_id") or "")
    line = re.sub(r"\D", "", normalized.get("digitable_line") or "")
    query = db.query(DdaBoleto).filter(
        DdaBoleto.tenant_id == user.tenant_id,
        DdaBoleto.company_id == company_id,
    )
    existing = None
    if line:
        existing = query.filter(DdaBoleto.digitable_line == line).first()
    if not existing and external_id:
        existing = query.filter(DdaBoleto.external_id == external_id).first()
    if existing:
        return existing, False

    beneficiary_document = re.sub(
        r"\D", "", normalized.get("beneficiary_document") or ""
    )
    amount = Decimal(str(normalized.get("amount") or 0))
    due_date = normalized["due_date"]
    supplier, invoice = _dda_find_matches(
        db,
        tenant_id=user.tenant_id,
        company_id=company_id,
        beneficiary_document=beneficiary_document,
        beneficiary_name=str(normalized.get("beneficiary_name") or ""),
        amount=amount,
    )
    analysis = dda_analyze_boleto(
        beneficiary_document=beneficiary_document,
        beneficiary_name=str(normalized.get("beneficiary_name") or ""),
        amount=amount,
        due_date=due_date,
        supplier_found=bool(supplier),
        invoice_found=bool(invoice),
        duplicated=False,
        overdue=due_date < date.today(),
    )

    row = DdaBoleto(
        tenant_id=user.tenant_id,
        company_id=company_id,
        connector_id=connector.id if connector else None,
        external_id=external_id,
        digitable_line=line,
        beneficiary_name=str(normalized.get("beneficiary_name") or ""),
        beneficiary_document=beneficiary_document,
        payer_document=re.sub(
            r"\D", "", normalized.get("payer_document") or ""
        ),
        issue_date=normalized.get("issue_date"),
        due_date=due_date,
        amount=amount,
        status="NOVO",
        bank_status=str(normalized.get("bank_status") or "EM_ABERTO"),
        invoice_id=invoice.id if invoice else None,
        match_score=analysis["score"],
        risk_level=analysis["risk_level"],
        recommendation=analysis["recommendation"],
        raw_json=json.dumps(
            normalized.get("raw") or normalized,
            ensure_ascii=False,
            default=str,
        ),
    )
    db.add(row)
    db.flush()

    can_auto_create = bool(
        connector
        and connector.auto_create_payable
        and (
            not connector.require_invoice_match
            or row.invoice_id is not None
        )
        and row.risk_level != "ALTO"
    )
    if can_auto_create:
        payable = Payable(
            tenant_id=user.tenant_id,
            company_id=company_id,
            supplier=row.beneficiary_name or "Beneficiário DDA",
            due_date=row.due_date,
            value=row.amount,
            status="EM ABERTO",
        )
        db.add(payable)
        db.flush()
        row.payable_id = payable.id
        row.status = "CONTA_CRIADA"

    return row, True


@app.get("/api/dda/providers")
def dda_providers(user: User = Depends(current_user)):
    return dda_provider_catalog()


@app.get("/api/dda/connectors")
def dda_connectors(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(DdaConnector).filter(
        DdaConnector.tenant_id == user.tenant_id,
        DdaConnector.company_id == company_id,
    ).order_by(DdaConnector.id.desc()).all()
    return [_dda_connector_payload(row) for row in rows]


@app.post("/api/dda/connectors")
def dda_save_connector(
    company_id: int,
    data: DdaConnectorIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    provider = data.provider.upper().strip()
    if provider not in DDA_SUPPORTED_PROVIDERS:
        raise HTTPException(400, "Banco ou provedor DDA não suportado")

    row = DdaConnector(
        tenant_id=user.tenant_id,
        company_id=company_id,
        provider=provider,
        name=data.name or DDA_SUPPORTED_PROVIDERS[provider]["label"],
        environment=data.environment.upper(),
        credentials_encrypted=_encrypt_secret(
            json.dumps(data.credentials, ensure_ascii=False)
        ) if data.credentials else "",
        active=data.active,
        auto_create_payable=data.auto_create_payable,
        require_invoice_match=data.require_invoice_match,
        last_status=(
            "PRONTO"
            if data.credentials
            else "AGUARDANDO_CREDENCIAIS"
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _dda_connector_payload(row)


@app.post("/api/dda/connectors/{connector_id}/sync")
def dda_sync_connector(
    connector_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    connector = db.query(DdaConnector).filter(
        DdaConnector.id == connector_id,
        DdaConnector.tenant_id == user.tenant_id,
    ).first()
    if not connector:
        raise HTTPException(404, "Conector DDA não encontrado")
    if not connector.active:
        raise HTTPException(400, "Ative o conector antes de sincronizar")
    if not connector.credentials_encrypted:
        raise HTTPException(400, "Cadastre as credenciais do banco")

    credentials = json.loads(
        _decrypt_secret(connector.credentials_encrypted)
    )
    try:
        items = dda_fetch_boletos(connector.provider, credentials)
        imported = 0
        duplicated = 0
        for item in items:
            _, created = _dda_upsert(
                db,
                user=user,
                company_id=connector.company_id,
                connector=connector,
                normalized=item,
            )
            if created:
                imported += 1
            else:
                duplicated += 1
        connector.last_sync_at = datetime.utcnow()
        connector.last_status = "OK"
        connector.last_message = (
            f"{imported} importado(s), {duplicated} duplicado(s)."
        )
        db.add(DdaSyncLog(
            tenant_id=user.tenant_id,
            company_id=connector.company_id,
            connector_id=connector.id,
            provider=connector.provider,
            status="OK",
            imported=imported,
            duplicated=duplicated,
            message=connector.last_message,
        ))
        db.commit()
        return {
            "ok": True,
            "imported": imported,
            "duplicated": duplicated,
        }
    except Exception as exc:
        db.rollback()
        connector = db.get(DdaConnector, connector_id)
        if connector:
            connector.last_sync_at = datetime.utcnow()
            connector.last_status = "ERRO"
            connector.last_message = str(exc)[:500]
            db.add(DdaSyncLog(
                tenant_id=user.tenant_id,
                company_id=connector.company_id,
                connector_id=connector.id,
                provider=connector.provider,
                status="ERRO",
                message=str(exc)[:500],
            ))
            db.commit()
        raise HTTPException(502, str(exc))


@app.post("/api/dda/boletos/manual")
def dda_manual_boleto(
    company_id: int,
    data: DdaManualBoletoIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    normalized = data.model_dump()
    normalized["beneficiary_document"] = re.sub(
        r"\D", "", data.beneficiary_document
    )
    normalized["payer_document"] = re.sub(
        r"\D", "", data.payer_document
    )
    row, created = _dda_upsert(
        db,
        user=user,
        company_id=company_id,
        connector=None,
        normalized=normalized,
    )
    if not created:
        raise HTTPException(409, "Este boleto já está cadastrado")
    db.commit()
    return _dda_boleto_payload(row)


@app.get("/api/dda/boletos")
def dda_list_boletos(
    company_id: int,
    status: str = "",
    search: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = db.query(DdaBoleto).filter(
        DdaBoleto.tenant_id == user.tenant_id,
        DdaBoleto.company_id == company_id,
    )
    if status:
        query = query.filter(DdaBoleto.status == status.upper())
    if search:
        term = f"%{search}%"
        query = query.filter(
            DdaBoleto.beneficiary_name.ilike(term)
            | DdaBoleto.beneficiary_document.ilike(term)
            | DdaBoleto.digitable_line.ilike(term)
        )
    rows = query.order_by(
        DdaBoleto.due_date.asc(),
        DdaBoleto.id.desc(),
    ).limit(1000).all()
    return [_dda_boleto_payload(row) for row in rows]


@app.get("/api/dda/summary")
def dda_summary(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(DdaBoleto).filter(
        DdaBoleto.tenant_id == user.tenant_id,
        DdaBoleto.company_id == company_id,
    ).all()
    today = date.today()
    open_rows = [
        row for row in rows
        if row.status not in {"PAGO", "IGNORADO"}
    ]
    return {
        "total_open": float(sum((row.amount for row in open_rows), Decimal("0"))),
        "count_open": len(open_rows),
        "overdue_value": float(sum(
            (row.amount for row in open_rows if row.due_date < today),
            Decimal("0"),
        )),
        "overdue_count": sum(1 for row in open_rows if row.due_date < today),
        "next_7_days_value": float(sum(
            (
                row.amount for row in open_rows
                if 0 <= (row.due_date - today).days <= 7
            ),
            Decimal("0"),
        )),
        "unmatched_count": sum(1 for row in open_rows if row.invoice_id is None),
        "high_risk_count": sum(1 for row in open_rows if row.risk_level == "ALTO"),
        "connectors": db.query(DdaConnector).filter(
            DdaConnector.tenant_id == user.tenant_id,
            DdaConnector.company_id == company_id,
        ).count(),
    }


@app.post("/api/dda/boletos/{boleto_id}/approve")
def dda_approve_boleto(
    boleto_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(DdaBoleto).filter(
        DdaBoleto.id == boleto_id,
        DdaBoleto.tenant_id == user.tenant_id,
    ).first()
    if not row:
        raise HTTPException(404, "Boleto não encontrado")
    if not row.payable_id:
        payable = Payable(
            tenant_id=user.tenant_id,
            company_id=row.company_id,
            supplier=row.beneficiary_name or "Beneficiário DDA",
            due_date=row.due_date,
            value=row.amount,
            status="EM ABERTO",
        )
        db.add(payable)
        db.flush()
        row.payable_id = payable.id
    row.status = "APROVADO"
    row.updated_at = datetime.utcnow()
    db.commit()
    return _dda_boleto_payload(row)


@app.post("/api/dda/boletos/{boleto_id}/ignore")
def dda_ignore_boleto(
    boleto_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(DdaBoleto).filter(
        DdaBoleto.id == boleto_id,
        DdaBoleto.tenant_id == user.tenant_id,
    ).first()
    if not row:
        raise HTTPException(404, "Boleto não encontrado")
    row.status = "IGNORADO"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.get("/api/dda/analysis")
def dda_analysis(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(DdaBoleto).filter(
        DdaBoleto.tenant_id == user.tenant_id,
        DdaBoleto.company_id == company_id,
        DdaBoleto.status.notin_(["PAGO", "IGNORADO"]),
    ).order_by(DdaBoleto.due_date.asc()).all()
    alerts = []
    for row in rows:
        if row.risk_level == "ALTO":
            alerts.append({
                "severity": "ALTO",
                "title": f"Revisar boleto de {row.beneficiary_name}",
                "detail": row.recommendation,
                "boleto_id": row.id,
            })
        elif row.due_date < date.today():
            alerts.append({
                "severity": "MEDIO",
                "title": f"Boleto vencido: {row.beneficiary_name}",
                "detail": f"Valor R$ {float(row.amount):.2f}",
                "boleto_id": row.id,
            })
        elif row.invoice_id is None:
            alerts.append({
                "severity": "MEDIO",
                "title": f"Sem NF-e vinculada: {row.beneficiary_name}",
                "detail": row.recommendation,
                "boleto_id": row.id,
            })
    return {
        "summary": (
            f"{len(rows)} boleto(s) em análise; "
            f"{sum(1 for r in rows if r.risk_level == 'ALTO')} de alto risco; "
            f"{sum(1 for r in rows if r.invoice_id is None)} sem NF-e vinculada."
        ),
        "alerts": alerts[:100],
    }

@app.post("/api/banking/import")
async def banking_import_statement(
    company_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    content = await file.read()
    digest = bank_file_hash(content)
    existing_import = db.query(BankStatementImport).filter(
        BankStatementImport.file_hash == digest,
        BankStatementImport.tenant_id == user.tenant_id,
        BankStatementImport.company_id == company_id,
    ).first()
    if existing_import:
        raise HTTPException(409, "Este extrato já foi importado")

    try:
        format_name, rows = bank_parse_statement(file.filename or "", content)
    except Exception as exc:
        raise HTTPException(400, str(exc))

    statement = BankStatementImport(
        tenant_id=user.tenant_id,
        company_id=company_id,
        file_name=file.filename or "extrato",
        file_hash=digest,
        format=format_name,
    )
    db.add(statement)
    db.flush()

    imported = 0
    duplicated = 0
    reconciled = 0

    for item in rows:
        duplicate = db.query(BankTransaction).filter(
            BankTransaction.tenant_id == user.tenant_id,
            BankTransaction.company_id == company_id,
            BankTransaction.external_id == item["external_id"],
        ).first()
        if duplicate:
            duplicated += 1
            continue

        amount = Decimal(str(item["amount"]))
        payable = None
        receivable = None
        dda = None

        if item["transaction_type"] == "DEBITO":
            payable = db.query(Payable).filter(
                Payable.tenant_id == user.tenant_id,
                Payable.company_id == company_id,
                Payable.value == amount,
                Payable.status != "PAGO",
            ).order_by(Payable.due_date.asc()).first()
            try:
                dda = db.query(DdaBoleto).filter(
                    DdaBoleto.tenant_id == user.tenant_id,
                    DdaBoleto.company_id == company_id,
                    DdaBoleto.amount == amount,
                    DdaBoleto.status.notin_(["PAGO", "IGNORADO"]),
                ).order_by(DdaBoleto.due_date.asc()).first()
            except Exception:
                db.rollback()
        else:
            receivable = db.query(Receivable).filter(
                Receivable.tenant_id == user.tenant_id,
                Receivable.company_id == company_id,
                Receivable.value == amount,
                Receivable.status == "EM ABERTO",
            ).order_by(Receivable.due_date.asc()).first()

        analysis = bank_analyze_match(
            transaction_type=item["transaction_type"],
            amount=amount,
            description=item["description"],
            payable_found=bool(payable),
            receivable_found=bool(receivable),
            dda_found=bool(dda),
        )

        row = BankTransaction(
            tenant_id=user.tenant_id,
            company_id=company_id,
            statement_import_id=statement.id,
            external_id=item["external_id"],
            transaction_date=item["transaction_date"],
            description=item["description"],
            document_number=item["document_number"],
            amount=amount,
            transaction_type=item["transaction_type"],
            category=item["category"],
            counterparty_name=item.get("counterparty_name", ""),
            counterparty_document=item.get("counterparty_document", ""),
            payable_id=payable.id if payable else None,
            receivable_id=receivable.id if receivable else None,
            dda_boleto_id=dda.id if dda else None,
            reconciliation_status=(
                "SUGERIDO" if analysis["score"] >= 50 else "PENDENTE"
            ),
            match_score=analysis["score"],
            recommendation=analysis["recommendation"],
            raw_json=json.dumps(item.get("raw", item), ensure_ascii=False, default=str),
        )
        db.add(row)
        imported += 1

        if analysis["score"] >= 80:
            if payable:
                payable.status = "PAGO"
            if receivable:
                receivable.status = "RECEBIDO"
            if dda:
                dda.status = "PAGO"
            row.reconciliation_status = "CONCILIADO"
            reconciled += 1

    statement.imported_count = imported
    statement.duplicate_count = duplicated
    db.add(BankReconciliationLog(
        tenant_id=user.tenant_id,
        company_id=company_id,
        action="IMPORT_STATEMENT",
        status="OK",
        message=(
            f"{imported} transação(ões), {duplicated} duplicada(s), "
            f"{reconciled} conciliada(s)."
        ),
    ))
    db.commit()
    return {
        "ok": True,
        "format": format_name,
        "imported": imported,
        "duplicated": duplicated,
        "reconciled": reconciled,
    }


@app.get("/api/banking/transactions")
def banking_transactions(
    company_id: int,
    status: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = db.query(BankTransaction).filter(
        BankTransaction.tenant_id == user.tenant_id,
        BankTransaction.company_id == company_id,
    )
    if status:
        query = query.filter(
            BankTransaction.reconciliation_status == status.upper()
        )
    rows = query.order_by(
        BankTransaction.transaction_date.desc(),
        BankTransaction.id.desc(),
    ).limit(1000).all()
    return [
        {
            "id": row.id,
            "date": row.transaction_date,
            "description": row.description,
            "amount": float(row.amount or 0),
            "transaction_type": row.transaction_type,
            "category": row.category,
            "payable_id": row.payable_id,
            "receivable_id": row.receivable_id,
            "dda_boleto_id": row.dda_boleto_id,
            "status": row.reconciliation_status,
            "match_score": row.match_score,
            "recommendation": row.recommendation,
        }
        for row in rows
    ]


@app.get("/api/banking/summary")
def banking_summary(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(BankTransaction).filter(
        BankTransaction.tenant_id == user.tenant_id,
        BankTransaction.company_id == company_id,
    ).all()
    credits = sum(
        (row.amount for row in rows if row.transaction_type == "CREDITO"),
        Decimal("0"),
    )
    debits = sum(
        (row.amount for row in rows if row.transaction_type == "DEBITO"),
        Decimal("0"),
    )
    return {
        "credits": float(credits),
        "debits": float(debits),
        "balance": float(credits - debits),
        "pending": sum(
            1 for row in rows if row.reconciliation_status == "PENDENTE"
        ),
        "suggested": sum(
            1 for row in rows if row.reconciliation_status == "SUGERIDO"
        ),
        "reconciled": sum(
            1 for row in rows if row.reconciliation_status == "CONCILIADO"
        ),
    }


@app.post("/api/banking/transactions/{transaction_id}/reconcile")
def banking_reconcile(
    transaction_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(BankTransaction).filter(
        BankTransaction.id == transaction_id,
        BankTransaction.tenant_id == user.tenant_id,
    ).first()
    if not row:
        raise HTTPException(404, "Transação não encontrada")

    if row.payable_id:
        payable = db.get(Payable, row.payable_id)
        if payable:
            payable.status = "PAGO"
    if row.receivable_id:
        receivable = db.get(Receivable, row.receivable_id)
        if receivable:
            receivable.status = "RECEBIDO"
    if row.dda_boleto_id:
        try:
            dda = db.get(DdaBoleto, row.dda_boleto_id)
            if dda:
                dda.status = "PAGO"
        except Exception:
            db.rollback()

    row.reconciliation_status = "CONCILIADO"
    db.commit()
    return {"ok": True}


@app.get("/api/banking/analysis")
def banking_analysis(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(BankTransaction).filter(
        BankTransaction.tenant_id == user.tenant_id,
        BankTransaction.company_id == company_id,
        BankTransaction.reconciliation_status != "CONCILIADO",
    ).order_by(BankTransaction.transaction_date.desc()).all()
    alerts = [
        {
            "transaction_id": row.id,
            "severity": "ALTO" if row.match_score < 50 else "MEDIO",
            "title": row.description or "Transação sem descrição",
            "detail": row.recommendation,
            "amount": float(row.amount or 0),
        }
        for row in rows[:100]
    ]
    return {
        "summary": (
            f"{len(rows)} transação(ões) aguardando conferência; "
            f"{sum(1 for row in rows if row.match_score < 50)} sem correspondência."
        ),
        "alerts": alerts,
    }

class PagBankConfigIn(BaseModel):
    environment: str = "SANDBOX"
    token: str = ""
    active: bool = True


class PagBankPixIn(BaseModel):
    receivable_id: int | None = None
    reference_id: str = ""
    amount: Decimal
    customer_name: str
    customer_email: EmailStr
    customer_tax_id: str
    expiration_minutes: int = 30


class PagBankBoletoIn(BaseModel):
    receivable_id: int | None = None
    reference_id: str = ""
    amount: Decimal
    due_date: date
    customer_name: str
    customer_email: EmailStr
    customer_tax_id: str
    phone_country: str = "55"
    phone_area: str
    phone_number: str
    address_street: str
    address_number: str
    address_locality: str
    address_city: str
    address_region: str
    address_postal_code: str


def _pagbank_config_payload(row: PagBankConfig | None) -> dict:
    if not row:
        return {
            "configured": False,
            "environment": "SANDBOX",
            "active": False,
            "last_status": "NAO_CONFIGURADO",
            "last_message": "",
        }
    return {
        "configured": bool(row.token_encrypted),
        "environment": row.environment,
        "active": row.active,
        "last_test_at": row.last_test_at,
        "last_status": row.last_status,
        "last_message": row.last_message,
    }


def _pagbank_payment_payload(row: PagBankPayment) -> dict:
    return {
        "id": row.id,
        "reference_id": row.reference_id,
        "order_id": row.order_id,
        "charge_id": row.charge_id,
        "payment_type": row.payment_type,
        "customer_name": row.customer_name,
        "customer_email": row.customer_email,
        "customer_tax_id": row.customer_tax_id,
        "amount": float(row.amount or 0),
        "status": row.status,
        "qr_code_text": row.qr_code_text,
        "qr_code_link": row.qr_code_link,
        "boleto_barcode": row.boleto_barcode,
        "boleto_pdf": row.boleto_pdf,
        "expires_at": row.expires_at,
        "paid_at": row.paid_at,
        "created_at": row.created_at,
    }


def _pagbank_get_config(
    db: Session,
    tenant_id: int,
    company_id: int,
) -> PagBankConfig:
    row = db.query(PagBankConfig).filter(
        PagBankConfig.tenant_id == tenant_id,
        PagBankConfig.company_id == company_id,
    ).first()
    if not row or not row.token_encrypted:
        raise HTTPException(400, "Configure o token do PagBank primeiro")
    if not row.active:
        raise HTTPException(400, "A integração PagBank está desativada")
    return row


@app.get("/api/pagbank/config")
def pagbank_get_config(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(PagBankConfig).filter(
        PagBankConfig.tenant_id == user.tenant_id,
        PagBankConfig.company_id == company_id,
    ).first()
    return _pagbank_config_payload(row)


@app.post("/api/pagbank/config")
def pagbank_save_config(
    company_id: int,
    data: PagBankConfigIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    environment = data.environment.upper()
    if environment not in {"SANDBOX", "PRODUCAO"}:
        raise HTTPException(400, "Ambiente inválido")

    row = db.query(PagBankConfig).filter(
        PagBankConfig.tenant_id == user.tenant_id,
        PagBankConfig.company_id == company_id,
    ).first()
    if not row:
        row = PagBankConfig(
            tenant_id=user.tenant_id,
            company_id=company_id,
        )
        db.add(row)

    row.environment = environment
    if data.token.strip():
        row.token_encrypted = _encrypt_secret(data.token.strip())
    row.active = data.active
    row.last_status = "CONFIGURADO" if row.token_encrypted else "SEM_TOKEN"
    row.last_message = "Configuração PagBank atualizada."
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _pagbank_config_payload(row)


@app.post("/api/pagbank/test")
def pagbank_test(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = _pagbank_get_config(
        db,
        user.tenant_id,
        company_id,
    )
    token = _decrypt_secret(row.token_encrypted)
    try:
        result = pagbank_test_connection(row.environment, token)
        row.last_test_at = datetime.utcnow()
        row.last_status = "OK"
        row.last_message = result["message"]
        db.commit()
        return result
    except Exception as exc:
        row.last_test_at = datetime.utcnow()
        row.last_status = "ERRO"
        row.last_message = str(exc)[:500]
        db.commit()
        raise HTTPException(502, str(exc))


@app.post("/api/pagbank/pix")
def pagbank_create_pix(
    company_id: int,
    data: PagBankPixIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    config = _pagbank_get_config(
        db,
        user.tenant_id,
        company_id,
    )
    token = _decrypt_secret(config.token_encrypted)
    reference_id = (
        data.reference_id.strip()
        or f"GF-{company_id}-{uuid.uuid4().hex[:16]}"
    )
    notification_url = str(
        request.base_url
    ).rstrip("/") + "/api/pagbank/webhook"

    try:
        payload = pagbank_create_pix_order(
            environment=config.environment,
            token=token,
            reference_id=reference_id,
            amount=data.amount,
            customer_name=data.customer_name,
            customer_email=str(data.customer_email),
            customer_tax_id=re.sub(r"\D", "", data.customer_tax_id),
            notification_url=notification_url,
            expiration_minutes=max(5, min(data.expiration_minutes, 1440)),
        )
    except Exception as exc:
        raise HTTPException(502, f"PagBank: {exc}")

    details = pagbank_extract_payment_details(payload)
    row = PagBankPayment(
        tenant_id=user.tenant_id,
        company_id=company_id,
        receivable_id=data.receivable_id,
        reference_id=reference_id,
        order_id=details["order_id"],
        charge_id=details["charge_id"],
        payment_type="PIX",
        customer_name=data.customer_name,
        customer_email=str(data.customer_email),
        customer_tax_id=re.sub(r"\D", "", data.customer_tax_id),
        amount=data.amount,
        status=details["status"],
        qr_code_text=details["qr_code_text"],
        qr_code_link=details["qr_code_link"],
        expires_at=datetime.utcnow() + timedelta(
            minutes=max(5, min(data.expiration_minutes, 1440))
        ),
        raw_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _pagbank_payment_payload(row)


@app.post("/api/pagbank/boleto")
def pagbank_create_boleto(
    company_id: int,
    data: PagBankBoletoIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    config = _pagbank_get_config(
        db,
        user.tenant_id,
        company_id,
    )
    token = _decrypt_secret(config.token_encrypted)
    reference_id = (
        data.reference_id.strip()
        or f"GF-BOL-{company_id}-{uuid.uuid4().hex[:12]}"
    )
    notification_url = str(
        request.base_url
    ).rstrip("/") + "/api/pagbank/webhook"

    customer = {
        "name": data.customer_name,
        "email": str(data.customer_email),
        "tax_id": re.sub(r"\D", "", data.customer_tax_id),
        "phones": [
            {
                "country": re.sub(r"\D", "", data.phone_country),
                "area": re.sub(r"\D", "", data.phone_area),
                "number": re.sub(r"\D", "", data.phone_number),
                "type": "MOBILE",
            }
        ],
        "address": {
            "street": data.address_street,
            "number": data.address_number,
            "locality": data.address_locality,
            "city": data.address_city,
            "region_code": data.address_region.upper(),
            "country": "BRA",
            "postal_code": re.sub(
                r"\D", "", data.address_postal_code
            ),
        },
    }
    try:
        payload = pagbank_create_boleto_order(
            environment=config.environment,
            token=token,
            reference_id=reference_id,
            amount=data.amount,
            customer=customer,
            due_date=data.due_date.isoformat(),
            notification_url=notification_url,
        )
    except Exception as exc:
        raise HTTPException(502, f"PagBank: {exc}")

    details = pagbank_extract_payment_details(payload)
    row = PagBankPayment(
        tenant_id=user.tenant_id,
        company_id=company_id,
        receivable_id=data.receivable_id,
        reference_id=reference_id,
        order_id=details["order_id"],
        charge_id=details["charge_id"],
        payment_type="BOLETO",
        customer_name=data.customer_name,
        customer_email=str(data.customer_email),
        customer_tax_id=re.sub(r"\D", "", data.customer_tax_id),
        amount=data.amount,
        status=details["status"],
        boleto_barcode=details["boleto_barcode"],
        boleto_pdf=details["boleto_pdf"],
        expires_at=datetime.combine(
            data.due_date,
            datetime.min.time(),
        ),
        raw_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _pagbank_payment_payload(row)


@app.get("/api/pagbank/payments")
def pagbank_list_payments(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(PagBankPayment).filter(
        PagBankPayment.tenant_id == user.tenant_id,
        PagBankPayment.company_id == company_id,
    ).order_by(
        PagBankPayment.id.desc()
    ).limit(500).all()
    return [_pagbank_payment_payload(row) for row in rows]


@app.post("/api/pagbank/payments/{payment_id}/refresh")
def pagbank_refresh_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(PagBankPayment).filter(
        PagBankPayment.id == payment_id,
        PagBankPayment.tenant_id == user.tenant_id,
    ).first()
    if not row:
        raise HTTPException(404, "Cobrança não encontrada")
    config = _pagbank_get_config(
        db,
        user.tenant_id,
        row.company_id,
    )
    token = _decrypt_secret(config.token_encrypted)
    try:
        payload = pagbank_get_order(
            config.environment,
            token,
            row.order_id,
        )
    except Exception as exc:
        raise HTTPException(502, f"PagBank: {exc}")

    details = pagbank_extract_payment_details(payload)
    row.status = details["status"]
    row.charge_id = details["charge_id"] or row.charge_id
    row.qr_code_text = details["qr_code_text"] or row.qr_code_text
    row.qr_code_link = details["qr_code_link"] or row.qr_code_link
    row.boleto_barcode = details["boleto_barcode"] or row.boleto_barcode
    row.boleto_pdf = details["boleto_pdf"] or row.boleto_pdf
    row.raw_json = json.dumps(payload, ensure_ascii=False, default=str)
    row.updated_at = datetime.utcnow()

    if pagbank_paid_status(row.status):
        row.paid_at = row.paid_at or datetime.utcnow()
        if row.receivable_id:
            receivable = db.get(Receivable, row.receivable_id)
            if receivable:
                receivable.status = "RECEBIDO"
                receivable.received_date = date.today()
    db.commit()
    return _pagbank_payment_payload(row)


@app.post("/api/pagbank/webhook")
async def pagbank_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    payload = await request.json()
    order_id = str(payload.get("id") or payload.get("order_id") or "")
    row = None
    if order_id:
        row = db.query(PagBankPayment).filter(
            PagBankPayment.order_id == order_id
        ).first()

    log = PagBankWebhookLog(
        tenant_id=row.tenant_id if row else None,
        company_id=row.company_id if row else None,
        order_id=order_id,
        event_type=str(payload.get("event") or payload.get("status") or ""),
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db.add(log)
    db.commit()

    if not row:
        log.message = "Cobrança não localizada; evento registrado."
        db.commit()
        return {"ok": True}

    config = db.query(PagBankConfig).filter(
        PagBankConfig.tenant_id == row.tenant_id,
        PagBankConfig.company_id == row.company_id,
    ).first()
    if not config or not config.token_encrypted:
        log.message = "Configuração PagBank não localizada."
        db.commit()
        return {"ok": True}

    try:
        verified = pagbank_get_order(
            config.environment,
            _decrypt_secret(config.token_encrypted),
            row.order_id,
        )
        details = pagbank_extract_payment_details(verified)
        row.status = details["status"]
        row.raw_json = json.dumps(
            verified,
            ensure_ascii=False,
            default=str,
        )
        row.updated_at = datetime.utcnow()
        if pagbank_paid_status(row.status):
            row.paid_at = row.paid_at or datetime.utcnow()
            if row.receivable_id:
                receivable = db.get(Receivable, row.receivable_id)
                if receivable:
                    receivable.status = "RECEBIDO"
                    receivable.received_date = date.today()
        log.processed = True
        log.message = "Evento validado consultando o pedido no PagBank."
        db.commit()
    except Exception as exc:
        log.message = f"Falha ao validar: {str(exc)[:400]}"
        db.commit()

    return {"ok": True}


@app.post("/api/pagbank/refresh-pending")
def pagbank_refresh_pending(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    config = _pagbank_get_config(
        db,
        user.tenant_id,
        company_id,
    )
    token = _decrypt_secret(config.token_encrypted)
    rows = db.query(PagBankPayment).filter(
        PagBankPayment.tenant_id == user.tenant_id,
        PagBankPayment.company_id == company_id,
    ).order_by(PagBankPayment.id.desc()).limit(100).all()

    updated = 0
    paid = 0
    errors = []
    for row in rows:
        if pagbank_paid_status(row.status):
            continue
        try:
            payload = pagbank_get_order(
                config.environment,
                token,
                row.order_id,
            )
            details = pagbank_extract_payment_details(payload)
            old_status = row.status
            row.status = details["status"]
            row.charge_id = details["charge_id"] or row.charge_id
            row.qr_code_text = details["qr_code_text"] or row.qr_code_text
            row.qr_code_link = details["qr_code_link"] or row.qr_code_link
            row.boleto_barcode = (
                details["boleto_barcode"] or row.boleto_barcode
            )
            row.boleto_pdf = details["boleto_pdf"] or row.boleto_pdf
            row.raw_json = json.dumps(
                payload,
                ensure_ascii=False,
                default=str,
            )
            row.updated_at = datetime.utcnow()
            if old_status != row.status:
                updated += 1

            if pagbank_paid_status(row.status):
                row.paid_at = row.paid_at or datetime.utcnow()
                paid += 1
                if row.receivable_id:
                    receivable = db.get(
                        Receivable,
                        row.receivable_id,
                    )
                    if receivable:
                        receivable.status = "RECEBIDO"
                        receivable.received_date = date.today()
        except Exception as exc:
            errors.append({
                "payment_id": row.id,
                "error": str(exc)[:300],
            })

    db.commit()
    return {
        "ok": True,
        "checked": len(rows),
        "updated": updated,
        "paid": paid,
        "errors": errors,
    }

@app.get("/api/pagbank/summary")
def pagbank_summary(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(PagBankPayment).filter(
        PagBankPayment.tenant_id == user.tenant_id,
        PagBankPayment.company_id == company_id,
    ).all()
    paid = [
        row for row in rows
        if pagbank_paid_status(row.status)
    ]
    pending = [
        row for row in rows
        if not pagbank_paid_status(row.status)
    ]
    return {
        "total_received": float(
            sum((row.amount for row in paid), Decimal("0"))
        ),
        "received_count": len(paid),
        "pending_value": float(
            sum((row.amount for row in pending), Decimal("0"))
        ),
        "pending_count": len(pending),
        "pix_count": sum(1 for row in rows if row.payment_type == "PIX"),
        "boleto_count": sum(1 for row in rows if row.payment_type == "BOLETO"),
    }

# --- Portal de revisão Shopee ---
SHOPEE_REVIEW_USER = "GestaoFacilERP"
SHOPEE_REVIEW_PASSWORD = "Gestao@2026"

_SHOPEE_REVIEW_LOGIN_HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GestaoFacilERP - Acesso de revisão</title>
  <style>
    *{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f4f5f7;color:#171717}
    .wrap{min-height:100vh;display:grid;grid-template-columns:1.05fr .95fr}
    .brand{background:linear-gradient(145deg,#101010,#2b1a0d);color:#fff;padding:64px;display:flex;align-items:center}
    .brandBox{max-width:620px}.logo{width:88px;height:88px;border-radius:24px;background:#111;border:1px solid #3b3b3b;display:flex;align-items:center;justify-content:center;font-size:38px;font-weight:900;color:#ff7a00;box-shadow:0 18px 45px #0007}
    h1{font-size:46px;margin:24px 0 10px}.orange{color:#ff7a00}.subtitle{font-size:18px;color:#d3d3d3;line-height:1.6}
    .chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:28px}.chip{padding:9px 13px;border:1px solid #ffffff22;border-radius:999px;color:#e7e7e7;background:#ffffff0b}
    .loginSide{padding:40px;display:flex;align-items:center;justify-content:center}.card{width:min(460px,100%);background:#fff;border-radius:22px;padding:34px;box-shadow:0 20px 60px #00000014;border:1px solid #e8e8e8}
    .eyebrow{font-size:12px;letter-spacing:.12em;font-weight:800;color:#ff7a00}.card h2{font-size:28px;margin:8px 0 6px}.muted{color:#747474;margin-bottom:24px;line-height:1.5}
    label{display:block;font-size:13px;font-weight:700;margin:14px 0 7px}.input{width:100%;height:48px;border:1px solid #d9d9d9;border-radius:11px;padding:0 13px;font-size:15px;outline:none}.input:focus{border-color:#ff7a00;box-shadow:0 0 0 3px #ff7a0018}
    button{width:100%;height:50px;border:0;border-radius:12px;background:#ff7a00;color:#111;font-weight:900;font-size:15px;margin-top:20px;cursor:pointer}.note{margin-top:18px;padding:13px;background:#fff7ed;border-radius:12px;color:#805119;font-size:12px;line-height:1.5}
    .error{padding:10px 12px;border-radius:10px;background:#fff0f0;color:#b42318;font-size:13px;margin-bottom:14px}
    @media(max-width:850px){.wrap{grid-template-columns:1fr}.brand{display:none}.loginSide{padding:20px}}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="brand">
      <div class="brandBox">
        <div class="logo">GF</div>
        <h1>Gestão <span class="orange">Fácil</span></h1>
        <div class="subtitle">ERP empresarial para estoque, vendas, financeiro, fiscal e integrações com marketplaces.</div>
        <div class="chips">
          <span class="chip">Estoque</span><span class="chip">Vendas</span><span class="chip">Financeiro</span>
          <span class="chip">NF-e / SEFAZ</span><span class="chip">Shopee</span><span class="chip">DDA / Bancos</span>
        </div>
      </div>
    </section>
    <section class="loginSide">
      <form class="card" method="post" action="/shopee-review/login">
        <div class="eyebrow">AMBIENTE DE REVISÃO</div>
        <h2>Acessar GestaoFacilERP</h2>
        <div class="muted">Acesso preparado exclusivamente para análise técnica da Shopee Open Platform.</div>
        {error}
        <label>Usuário</label>
        <input class="input" name="username" autocomplete="username" required placeholder="Digite o usuário de teste">
        <label>Senha</label>
        <input class="input" type="password" name="password" autocomplete="current-password" required placeholder="Digite a senha de teste">
        <button type="submit">Entrar no sistema</button>
        <div class="note">Este ambiente demonstra a interface e os módulos do ERP sem expor dados sensíveis da operação real.</div>
      </form>
    </section>
  </div>
</body>
</html>
"""

_SHOPEE_REVIEW_APP_HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GestaoFacilERP - Revisão Shopee</title>
  <style>
    *{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f4f5f7;color:#151515}
    .shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}.side{background:#111;color:#eee;padding:20px 15px}.brand{display:flex;gap:10px;align-items:center;font-weight:900;font-size:18px;margin-bottom:28px}.mark{background:#ff7a00;color:#111;border-radius:10px;width:34px;height:34px;display:grid;place-items:center}
    .group{font-size:10px;color:#777;letter-spacing:.12em;margin:21px 10px 8px}.nav{padding:11px 12px;border-radius:10px;margin:4px 0;color:#ccc}.nav.on{background:#412612;color:#fff;border-left:3px solid #ff7a00}
    main{padding:26px}.top{display:flex;justify-content:space-between;align-items:center}.badge{padding:8px 12px;border-radius:999px;background:#eaf8ef;color:#15723b;font-weight:800;font-size:12px}
    h1{margin:18px 0 4px;font-size:30px}.muted{color:#727272}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.card{background:#fff;border:1px solid #e5e5e5;border-radius:16px;padding:18px;box-shadow:0 8px 25px #00000008}.k{font-size:13px;color:#777}.v{font-size:25px;font-weight:900;margin-top:7px}.orange{color:#ff7a00}
    .two{display:grid;grid-template-columns:1.35fr .65fr;gap:14px}.module{min-height:245px}.row{display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid #eee}.ok{color:#138a4b;font-weight:800}.warn{color:#b56c00;font-weight:800}
    .flow{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.pill{background:#f6f6f6;padding:10px 12px;border-radius:10px;font-size:13px;border:1px solid #ededed}.footer{margin-top:16px;font-size:12px;color:#888}
    @media(max-width:900px){.shell{grid-template-columns:1fr}.side{display:none}.grid{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="shell">
  <aside class="side">
    <div class="brand"><div class="mark">GF</div>Gestão Fácil</div>
    <div class="group">GERAL</div><div class="nav on">Dashboard</div>
    <div class="group">ESTOQUE</div><div class="nav">Produtos</div><div class="nav">Estoque</div><div class="nav">Estoque inteligente</div>
    <div class="group">FINANCEIRO</div><div class="nav">Vendas</div><div class="nav">Conciliação Bancária</div><div class="nav">DDA Inteligente</div><div class="nav">Contas a pagar</div><div class="nav">Contas a receber</div>
    <div class="group">MARKETPLACES</div><div class="nav">Shopee</div>
  </aside>
  <main>
    <div class="top"><div><b>GestaoFacilERP</b><div class="muted" style="font-size:12px">Ambiente de revisão técnica</div></div><span class="badge">● Ambiente disponível</span></div>
    <h1>Dashboard</h1><div class="muted">Visão consolidada da operação e das integrações.</div>
    <section class="grid">
      <div class="card"><div class="k">Vendas hoje</div><div class="v">R$ 0,00</div><div class="muted">Ambiente de demonstração</div></div>
      <div class="card"><div class="k">Pedidos Shopee</div><div class="v orange">Integração</div><div class="muted">Open API V2.0</div></div>
      <div class="card"><div class="k">Estoque</div><div class="v">Sincronizado</div><div class="muted">Produtos e quantidades</div></div>
      <div class="card"><div class="k">Fiscal</div><div class="v">SEFAZ</div><div class="muted">NF-e e documentos</div></div>
    </section>
    <section class="two">
      <div class="card module">
        <h3>Fluxo planejado da integração Shopee</h3>
        <div class="flow">
          <span class="pill">1. Autorizar loja</span><span class="pill">2. Importar pedidos</span>
          <span class="pill">3. Sincronizar produtos</span><span class="pill">4. Atualizar estoque</span>
          <span class="pill">5. Consultar logística</span><span class="pill">6. Atualizar status</span>
        </div>
        <div class="footer">A integração utiliza autenticação e autorização da Shopee Open Platform e é destinada às lojas próprias da empresa.</div>
      </div>
      <div class="card module">
        <h3>Módulos</h3>
        <div class="row"><span>Produtos</span><span class="ok">Disponível</span></div>
        <div class="row"><span>Estoque</span><span class="ok">Disponível</span></div>
        <div class="row"><span>Pedidos</span><span class="ok">Disponível</span></div>
        <div class="row"><span>Shopee</span><span class="warn">Em integração</span></div>
        <div class="row"><span>Financeiro</span><span class="ok">Disponível</span></div>
      </div>
    </section>
  </main>
</div>
</body>
</html>
"""

@app.get("/shopee-review", response_class=HTMLResponse, include_in_schema=False)
def shopee_review_login():
    return HTMLResponse(_SHOPEE_REVIEW_LOGIN_HTML.format(error=""))

@app.post("/shopee-review/login", response_class=HTMLResponse, include_in_schema=False)
def shopee_review_login_submit(
    username: str = Form(...),
    password: str = Form(...),
):
    if username == SHOPEE_REVIEW_USER and password == SHOPEE_REVIEW_PASSWORD:
        return HTMLResponse(_SHOPEE_REVIEW_APP_HTML)
    error = '<div class="error">Usuário ou senha inválidos.</div>'
    return HTMLResponse(_SHOPEE_REVIEW_LOGIN_HTML.format(error=error), status_code=401)

@app.get("/health")
def health():
    return {"status": "ok"}

# --- Integração SEFAZ / Distribuição DF-e ---
from app.models import SefazDistributionConfig, SefazDistributionDocument
from app.services.sefaz_distribution import load_certificate_info, query_distribution, query_by_access_key, summarize_document
from app.services.sefaz_import import import_sefaz_document
from app.services.sefaz_manifestation import manifest_science

UF_CODES = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27,
    "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41,
    "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}


class SefazConfigIn(BaseModel):
    environment: str = "PRODUCAO"
    automatic_import: bool = False


class SefazAccessKeyIn(BaseModel):
    access_key: str


@app.get("/api/sefaz/config")
def sefaz_config(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    config = db.query(SefazDistributionConfig).filter(
        SefazDistributionConfig.tenant_id == user.tenant_id,
        SefazDistributionConfig.company_id == company_id,
    ).first()
    fiscal = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()

    pending = db.query(func.count(SefazDistributionDocument.id)).filter(
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
        SefazDistributionDocument.status.in_(["RECEBIDO", "AGUARDANDO_MANIFESTACAO", "MANIFESTADO_AGUARDANDO_XML"]),
    ).scalar() or 0
    imported = db.query(func.count(SefazDistributionDocument.id)).filter(
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
        SefazDistributionDocument.status.in_(["IMPORTADO", "DUPLICADA"]),
    ).scalar() or 0

    return {
        "environment": config.environment if config else "PRODUCAO",
        "last_nsu": config.last_nsu if config else "000000000000000",
        "max_nsu": config.max_nsu if config else "000000000000000",
        "automatic_import": config.automatic_import if config else False,
        "last_query_at": config.last_query_at if config else None,
        "last_status_code": config.last_status_code if config else "",
        "last_status_message": config.last_status_message if config else "",
        "cooldown_seconds": (
            _remaining_sefaz_cooldown(config, datetime.utcnow()) if config else 0
        ),
        "next_query_at": (
            config.last_query_at + _SEFAZ_COOLDOWN
            if config and _remaining_sefaz_cooldown(config, datetime.utcnow()) > 0
            else None
        ),
        "has_certificate": bool(fiscal and fiscal.certificate_path),
        "company_cnpj": company.cnpj,
        "company_state": company.state,
        "pending_documents": int(pending),
        "imported_documents": int(imported),
    }


@app.put("/api/sefaz/config")
def sefaz_save(
    company_id: int,
    data: SefazConfigIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    environment = data.environment.upper()
    if environment not in {"PRODUCAO", "HOMOLOGACAO"}:
        raise HTTPException(400, "Ambiente inválido")

    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    config = db.query(SefazDistributionConfig).filter(
        SefazDistributionConfig.tenant_id == user.tenant_id,
        SefazDistributionConfig.company_id == company_id,
    ).first()
    if not config:
        config = SefazDistributionConfig(
            tenant_id=user.tenant_id,
            company_id=company_id,
        )
        db.add(config)

    config.environment = environment
    config.automatic_import = data.automatic_import
    config.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.get("/api/sefaz/certificate/test")
def sefaz_cert_test(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    fiscal = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not fiscal or not fiscal.certificate_path:
        raise HTTPException(400, "Cadastre o certificado A1 primeiro")

    try:
        info = load_certificate_info(
            fiscal.certificate_path,
            _decrypt_secret(fiscal.certificate_password_encrypted),
        )
    except Exception as exc:
        raise HTTPException(400, f"Certificado inválido: {exc}")

    subject_digits = re.sub(r"\D", "", info.subject)
    company_cnpj = re.sub(r"\D", "", company.cnpj or "")
    cnpj_matches = not company_cnpj or company_cnpj in subject_digits
    expired = info.valid_until < datetime.utcnow()

    return {
        "ok": not expired,
        "subject": info.subject,
        "issuer": info.issuer,
        "serial_number": info.serial_number,
        "valid_from": info.valid_from,
        "valid_until": info.valid_until,
        "expired": expired,
        "cnpj_matches": cnpj_matches,
        "company_cnpj": company_cnpj,
    }


def _document_payload(row: SefazDistributionDocument) -> dict:
    return {
        "id": row.id,
        "nsu": row.nsu,
        "schema_name": row.schema_name,
        "access_key": row.access_key,
        "document_type": row.document_type,
        "issuer_name": row.issuer_name,
        "issuer_document": row.issuer_document,
        "issue_date": row.issue_date,
        "total_value": float(row.total_value or 0),
        "status": row.status,
        "created_at": row.created_at,
    }


@app.post("/api/sefaz/query")
def sefaz_query(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")
    if len(re.sub(r"\D", "", company.cnpj or "")) != 14:
        raise HTTPException(400, "Cadastre um CNPJ válido na empresa")

    uf_code = UF_CODES.get((company.state or "").upper())
    if not uf_code:
        raise HTTPException(400, "Cadastre uma UF válida na empresa")

    fiscal = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not fiscal or not fiscal.certificate_path:
        raise HTTPException(400, "Cadastre o certificado A1")

    config = db.query(SefazDistributionConfig).filter(
        SefazDistributionConfig.tenant_id == user.tenant_id,
        SefazDistributionConfig.company_id == company_id,
    ).first()
    if not config:
        config = SefazDistributionConfig(
            tenant_id=user.tenant_id,
            company_id=company_id,
        )
        db.add(config)
        db.flush()

    query_lock = _sefaz_lock(user.tenant_id, company_id)
    if not query_lock.acquire(blocking=False):
        raise HTTPException(409, "Já existe uma consulta à SEFAZ em andamento para esta empresa")

    try:
        now = datetime.utcnow()
        remaining = _remaining_sefaz_cooldown(config, now)
        if remaining > 0:
            minutes = max(1, (remaining + 59) // 60)
            raise HTTPException(
                429,
                f"A SEFAZ exige intervalo antes de uma nova consulta. Aguarde aproximadamente {minutes} minuto(s).",
            )

        sent_nsu = _normalized_nsu(config.last_nsu)
        try:
            result = query_distribution(
                company.cnpj,
                uf_code,
                sent_nsu,
                config.environment,
                fiscal.certificate_path,
                _decrypt_secret(fiscal.certificate_password_encrypted),
            )
        except Exception as exc:
            config.last_query_at = now
            config.last_status_code = "ERRO"
            config.last_status_message = str(exc)[:500]
            db.commit()
            raise HTTPException(502, f"Falha ao consultar a SEFAZ: {exc}")

        status_code = str(result.get("status_code", ""))
        status_message = str(result.get("status_message", ""))

        # O cStat 656 não deve alterar o NSU persistido. Alterá-lo faria a
        # próxima solicitação usar um valor diferente do último NSU válido.
        if status_code == "656":
            config.last_query_at = now
            config.last_status_code = status_code
            config.last_status_message = status_message[:500]
            db.commit()
            raise HTTPException(
                429,
                "SEFAZ 656 - Consumo indevido. O NSU válido foi preservado; aguarde 1 hora antes de consultar novamente.",
            )

        folder = (
            Path(__file__).resolve().parent.parent
            / "uploads"
            / "sefaz"
            / str(user.tenant_id)
            / str(company_id)
        )
        folder.mkdir(parents=True, exist_ok=True)

        saved_rows: list[SefazDistributionDocument] = []
        for item in result["documents"]:
            existing = db.query(SefazDistributionDocument).filter(
                SefazDistributionDocument.tenant_id == user.tenant_id,
                SefazDistributionDocument.company_id == company_id,
                SefazDistributionDocument.nsu == item["nsu"],
            ).first()
            if existing:
                continue

            summary = summarize_document(item["xml"])
            path = folder / f"{item['nsu']}_{uuid.uuid4().hex}.xml"
            path.write_bytes(item["xml"])
            is_summary = summary["document_type"] == "RESNFE"
            row = SefazDistributionDocument(
                tenant_id=user.tenant_id,
                company_id=company_id,
                nsu=item["nsu"],
                schema_name=item["schema"],
                access_key=summary["access_key"],
                document_type=summary["document_type"],
                issuer_name=summary["issuer_name"],
                issuer_document=summary["issuer_document"],
                issue_date=summary["issue_date"],
                total_value=summary["total_value"],
                xml_path=str(path),
                status="AGUARDANDO_MANIFESTACAO" if is_summary else "RECEBIDO",
            )
            db.add(row)
            db.flush()
            saved_rows.append(row)

        returned_last = _normalized_nsu(result.get("last_nsu"))
        returned_max = _normalized_nsu(result.get("max_nsu"))
        current_last = _normalized_nsu(config.last_nsu)
        current_max = _normalized_nsu(config.max_nsu)

        # Nunca retrocede nem zera o NSU por causa de resposta incompleta.
        if status_code in {"137", "138"}:
            if int(returned_last) >= int(current_last):
                config.last_nsu = returned_last
            if returned_max != "000000000000000" and int(returned_max) >= int(current_max):
                config.max_nsu = returned_max

        config.last_query_at = now
        config.last_status_code = status_code
        config.last_status_message = status_message[:500]
        db.commit()

        imported = 0
        import_errors = 0
        if config.automatic_import:
            for row in saved_rows:
                if row.status != "RECEBIDO":
                    continue
                try:
                    output = import_sefaz_document(
                        db, row, user.tenant_id, company_id
                    )
                    if output.get("ok"):
                        imported += 1
                except Exception:
                    db.rollback()
                    current_row = db.query(SefazDistributionDocument).filter(
                        SefazDistributionDocument.id == row.id
                    ).first()
                    if current_row:
                        current_row.status = "ERRO"
                        db.commit()
                    import_errors += 1

        return {
            "ok": True,
            "documents_saved": len(saved_rows),
            "documents_imported": imported,
            "import_errors": import_errors,
            "status_code": status_code,
            "status_message": status_message,
            "last_nsu": config.last_nsu,
            "max_nsu": config.max_nsu,
            "next_query_after_seconds": (
                3600 if status_code == "137" or config.last_nsu == config.max_nsu else 0
            ),
            "sent_nsu": sent_nsu,
        }
    finally:
        query_lock.release()



@app.post("/api/sefaz/query-key")
def sefaz_query_key(
    company_id: int,
    data: SefazAccessKeyIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    access_key = re.sub(r"\D", "", data.access_key or "")
    if len(access_key) != 44:
        raise HTTPException(400, "A chave de acesso deve conter 44 números")

    uf_code = UF_CODES.get((company.state or "").upper())
    if not uf_code:
        raise HTTPException(400, "Cadastre uma UF válida na empresa")

    fiscal = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not fiscal or not fiscal.certificate_path:
        raise HTTPException(400, "Cadastre o certificado A1")

    config = db.query(SefazDistributionConfig).filter(
        SefazDistributionConfig.tenant_id == user.tenant_id,
        SefazDistributionConfig.company_id == company_id,
    ).first()
    environment = config.environment if config else "PRODUCAO"

    try:
        result = query_by_access_key(
            company.cnpj,
            uf_code,
            access_key,
            environment,
            fiscal.certificate_path,
            _decrypt_secret(fiscal.certificate_password_encrypted),
        )
    except Exception as exc:
        raise HTTPException(502, f"Falha ao consultar a chave na SEFAZ: {exc}")

    folder = (
        Path(__file__).resolve().parent.parent
        / "uploads"
        / "sefaz"
        / str(user.tenant_id)
        / str(company_id)
    )
    folder.mkdir(parents=True, exist_ok=True)

    saved = []
    for item in result["documents"]:
        summary = summarize_document(item["xml"])
        existing = db.query(SefazDistributionDocument).filter(
            SefazDistributionDocument.tenant_id == user.tenant_id,
            SefazDistributionDocument.company_id == company_id,
            (
                (SefazDistributionDocument.access_key == summary["access_key"])
                if summary["access_key"]
                else (SefazDistributionDocument.nsu == item["nsu"])
            ),
        ).first()
        if existing:
            saved.append(existing)
            continue

        path = folder / f"{item['nsu'] or 'chave'}_{uuid.uuid4().hex}.xml"
        path.write_bytes(item["xml"])
        is_summary = summary["document_type"] == "RESNFE"
        row = SefazDistributionDocument(
            tenant_id=user.tenant_id,
            company_id=company_id,
            nsu=item["nsu"],
            schema_name=item["schema"],
            access_key=summary["access_key"] or access_key,
            document_type=summary["document_type"],
            issuer_name=summary["issuer_name"],
            issuer_document=summary["issuer_document"],
            issue_date=summary["issue_date"],
            total_value=summary["total_value"],
            xml_path=str(path),
            status="AGUARDANDO_MANIFESTACAO" if is_summary else "RECEBIDO",
        )
        db.add(row)
        db.flush()
        saved.append(row)
    db.commit()

    detail = ""
    if result["status_code"] == "137":
        detail = (
            "A SEFAZ não disponibilizou essa chave para este CNPJ/certificado. "
            "Quando o primeiro uso do serviço ocorre depois da emissão, o Ambiente Nacional "
            "pode não gerar NSU retroativo; nesse caso importe o XML recebido do fornecedor."
        )

    return {
        "ok": True,
        "status_code": result["status_code"],
        "status_message": result["status_message"],
        "documents_saved": len(saved),
        "detail": detail,
    }


@app.post("/api/sefaz/manifest-pending")
def sefaz_manifest_pending(
    company_id: int,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    company = db.query(Company).filter(
        Company.id == company_id,
        Company.tenant_id == user.tenant_id,
    ).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")

    fiscal = db.query(FiscalConfig).filter(
        FiscalConfig.tenant_id == user.tenant_id,
        FiscalConfig.company_id == company_id,
    ).first()
    if not fiscal or not fiscal.certificate_path:
        raise HTTPException(400, "Cadastre o certificado A1")

    config = db.query(SefazDistributionConfig).filter(
        SefazDistributionConfig.tenant_id == user.tenant_id,
        SefazDistributionConfig.company_id == company_id,
    ).first()
    if not config:
        raise HTTPException(
            400,
            "Salve a configuração da SEFAZ primeiro",
        )

    limit = max(1, min(int(limit or 10), 20))
    rows = db.query(SefazDistributionDocument).filter(
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
        SefazDistributionDocument.status.in_([
            "AGUARDANDO_MANIFESTACAO",
            "MANIFESTADO_AGUARDANDO_XML",
        ]),
    ).order_by(
        SefazDistributionDocument.id.asc()
    ).limit(limit).all()

    password = _decrypt_secret(
        fiscal.certificate_password_encrypted
    )
    manifested = 0
    xml_released = 0
    imported = 0
    waiting = 0
    errors = []

    folder = (
        Path(__file__).resolve().parent.parent
        / "uploads"
        / "sefaz"
        / str(user.tenant_id)
        / str(company_id)
    )
    folder.mkdir(parents=True, exist_ok=True)

    for row in rows:
        key = re.sub(r"\D", "", row.access_key or "")
        if len(key) != 44:
            errors.append({
                "id": row.id,
                "error": "Chave inválida no resumo",
            })
            continue

        try:
            if row.status == "AGUARDANDO_MANIFESTACAO":
                event = manifest_science(
                    company.cnpj,
                    key,
                    config.environment,
                    fiscal.certificate_path,
                    password,
                )
                if not event.get("accepted"):
                    errors.append({
                        "id": row.id,
                        "code": event.get("status_code"),
                        "error": event.get("status_message"),
                    })
                    continue
                row.status = "MANIFESTADO_AGUARDANDO_XML"
                db.commit()
                manifested += 1

            result = query_by_access_key(
                company.cnpj,
                UF_CODES.get(
                    (company.state or "").upper(),
                    35,
                ),
                key,
                config.environment,
                fiscal.certificate_path,
                password,
            )

            full_item = None
            full_summary = None
            for item in result.get("documents", []):
                summary = summarize_document(item["xml"])
                if summary.get("document_type") != "RESNFE":
                    full_item = item
                    full_summary = summary
                    break

            if full_item is None:
                waiting += 1
                continue

            xml_path = folder / (
                f"{full_item.get('nsu') or row.nsu}_"
                f"{uuid.uuid4().hex}.xml"
            )
            xml_path.write_bytes(full_item["xml"])

            row.nsu = full_item.get("nsu") or row.nsu
            row.schema_name = full_item.get("schema", "")
            row.document_type = full_summary.get(
                "document_type",
                "PROCNFE",
            )
            row.access_key = (
                full_summary.get("access_key") or key
            )
            row.issuer_name = full_summary.get(
                "issuer_name",
                "",
            )
            row.issuer_document = full_summary.get(
                "issuer_document",
                "",
            )
            row.issue_date = full_summary.get("issue_date")
            row.total_value = full_summary.get(
                "total_value",
                Decimal("0"),
            )
            row.xml_path = str(xml_path)
            row.status = "RECEBIDO"
            db.commit()
            xml_released += 1

            if config.automatic_import:
                output = import_sefaz_document(
                    db,
                    row,
                    user.tenant_id,
                    company_id,
                )
                if output.get("ok") or output.get("duplicate"):
                    imported += 1
        except Exception as exc:
            db.rollback()
            errors.append({
                "id": row.id,
                "error": str(exc)[:500],
            })

    remaining = db.query(
        func.count(SefazDistributionDocument.id)
    ).filter(
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
        SefazDistributionDocument.status.in_([
            "AGUARDANDO_MANIFESTACAO",
            "MANIFESTADO_AGUARDANDO_XML",
        ]),
    ).scalar() or 0

    return {
        "ok": True,
        "processed": len(rows),
        "manifested": manifested,
        "xml_released": xml_released,
        "imported": imported,
        "waiting": waiting,
        "remaining": int(remaining),
        "errors": errors,
    }

@app.get("/api/sefaz/documents")
def sefaz_documents(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(SefazDistributionDocument).filter(
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
    ).order_by(SefazDistributionDocument.id.desc()).limit(500).all()
    return [_document_payload(row) for row in rows]


@app.post("/api/sefaz/documents/{document_id}/import")
def sefaz_import_one(
    document_id: int,
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    row = db.query(SefazDistributionDocument).filter(
        SefazDistributionDocument.id == document_id,
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
    ).first()
    if not row:
        raise HTTPException(404, "Documento da SEFAZ não encontrado")
    try:
        return import_sefaz_document(db, row, user.tenant_id, company_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Falha ao importar NF-e: {exc}")


@app.post("/api/sefaz/import-all")
def sefaz_import_all(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(SefazDistributionDocument).filter(
        SefazDistributionDocument.tenant_id == user.tenant_id,
        SefazDistributionDocument.company_id == company_id,
        SefazDistributionDocument.status == "RECEBIDO",
    ).order_by(SefazDistributionDocument.id.asc()).all()

    imported = 0
    duplicates = 0
    errors: list[dict] = []
    for row in rows:
        try:
            output = import_sefaz_document(db, row, user.tenant_id, company_id)
            if output.get("duplicate"):
                duplicates += 1
            elif output.get("ok"):
                imported += 1
        except Exception as exc:
            db.rollback()
            errors.append({"document_id": row.id, "error": str(exc)})

    return {
        "ok": True,
        "processed": len(rows),
        "imported": imported,
        "duplicates": duplicates,
        "errors": errors,
    }

@app.get("/api/ai/status")
def ai_status(user: User = Depends(current_user)):
    import os
    return {"configured": True, "read_only": True, "mode": "LOCAL_SEM_CUSTO", "provider": "Gestao Facil"}

@app.post("/api/ai/ask")
def ai_ask(data: AiQuestionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    company = db.query(Company).filter(Company.id == data.company_id, Company.tenant_id == user.tenant_id).first()
    if not company:
        raise HTTPException(404, "Empresa não encontrada")
    question = data.question.strip()
    if not question:
        raise HTTPException(400, "Digite uma pergunta")
    try:
        context = build_company_context(db, user.tenant_id, data.company_id, question)
        answer = answer_question(context, question, data.history)
        return {"answer": answer, "read_only": True}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Falha ao consultar a IA: {exc}")

