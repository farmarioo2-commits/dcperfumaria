import re
import math
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from datetime import date
from threading import Lock
from decimal import Decimal
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr
from sqlalchemy import case, func
from sqlalchemy.orm import Session
from pypdf import PdfReader
from cryptography.fernet import Fernet
import base64
import xml.etree.ElementTree as ET

from app.core.security import create_token, decode_token, hash_password, verify_password
from app.db.session import Base, engine, get_db
from app.models import Company, Customer, FiscalConfig, FiscalDocument, GmailImportLog, ImportedNfe, ImportedNfeItem, ImportedPdf, NfeInstallment, Payable, Product, Receivable, Sale, SaleItem, StockMovement, Supplier, Tenant, User
from app.services.gmail_nfe_import import gmail_is_configured, sync_once
from app.services.ai_assistant import answer_question, build_company_context

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
        if stock <= (product.minimum_stock or 0):
            low_stock_items.append({
                "id": product.id,
                "name": product.name,
                "stock": stock,
                "minimum_stock": product.minimum_stock or 0,
            })

    sales_month_query = db.query(
        func.coalesce(func.sum(Sale.total), 0),
        func.count(Sale.id),
    ).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= month_start,
        Sale.sale_date <= today,
        Sale.status == "CONCLUÍDA",
    ).one()

    sales_today_query = db.query(
        func.coalesce(func.sum(Sale.total), 0),
        func.count(Sale.id),
    ).filter(
        Sale.tenant_id == user.tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date == today,
        Sale.status == "CONCLUÍDA",
    ).one()

    payables_open = db.query(func.coalesce(func.sum(Payable.value), 0)).filter(
        Payable.tenant_id == user.tenant_id,
        Payable.company_id == company_id,
        Payable.status != "PAGO",
    ).scalar() or 0

    receivables_open = db.query(func.coalesce(func.sum(Receivable.value), 0)).filter(
        Receivable.tenant_id == user.tenant_id,
        Receivable.company_id == company_id,
        Receivable.status == "EM ABERTO",
    ).scalar() or 0

    overdue_query = db.query(
        func.coalesce(func.sum(Payable.value), 0),
        func.count(Payable.id),
    ).filter(
        Payable.tenant_id == user.tenant_id,
        Payable.company_id == company_id,
        Payable.status != "PAGO",
        Payable.due_date < today,
    ).one()

    # Série dos últimos seis meses, compatível com SQLite e PostgreSQL.
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

    sefaz_pending = 0
    try:
        sefaz_pending = db.query(func.count(SefazDistributionDocument.id)).filter(
            SefazDistributionDocument.tenant_id == user.tenant_id,
            SefazDistributionDocument.company_id == company_id,
            SefazDistributionDocument.status == "RECEBIDO",
        ).scalar() or 0
    except Exception:
        db.rollback()

    return {
        # Campos antigos preservados para compatibilidade.
        "products": len(products),
        "stock_units": stock_units,
        "stock_value": stock_value,
        "low_stock": len(low_stock_items),
        "sales_month": float(sales_month_query[0] or 0),
        "payables_month": float(payables_open),
        "receivables_open": float(receivables_open),
        "received_month": float(
            db.query(func.coalesce(func.sum(Receivable.value), 0)).filter(
                Receivable.tenant_id == user.tenant_id,
                Receivable.company_id == company_id,
                Receivable.status == "RECEBIDO",
                Receivable.received_date >= month_start,
                Receivable.received_date <= today,
            ).scalar() or 0
        ),

        # Dashboard inteligente.
        "sales_today": float(sales_today_query[0] or 0),
        "sales_today_count": int(sales_today_query[1] or 0),
        "sales_month_count": int(sales_month_query[1] or 0),
        "payables_open": float(payables_open),
        "overdue_payables_count": int(overdue_query[1] or 0),
        "overdue_payables_value": float(overdue_query[0] or 0),
        "sefaz_pending": int(sefaz_pending),
        "sales_chart": sales_chart,
        "low_stock_items": low_stock_items[:8],
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

@app.get("/health")
def health():
    return {"status": "ok"}

# --- Integração SEFAZ / Distribuição DF-e ---
from app.models import SefazDistributionConfig, SefazDistributionDocument
from app.services.sefaz_distribution import load_certificate_info, query_distribution, query_by_access_key, summarize_document
from app.services.sefaz_import import import_sefaz_document

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
        SefazDistributionDocument.status.in_(["RECEBIDO", "AGUARDANDO_MANIFESTACAO"]),
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

