from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base

class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(180))
    slug: Mapped[str] = mapped_column(String(120), unique=True)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    email: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(40), default="ADMIN")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    trade_name: Mapped[str] = mapped_column(String(180))
    legal_name: Mapped[str] = mapped_column(String(220), default="")
    cnpj: Mapped[str] = mapped_column(String(14), default="")
    state_registration: Mapped[str] = mapped_column(String(30), default="")
    municipal_registration: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(180), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(String(220), default="")
    number: Mapped[str] = mapped_column(String(30), default="")
    complement: Mapped[str] = mapped_column(String(120), default="")
    district: Mapped[str] = mapped_column(String(120), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(2), default="SP")
    zip_code: Mapped[str] = mapped_column(String(10), default="")

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    sku: Mapped[str] = mapped_column(String(80))
    barcode: Mapped[str] = mapped_column(String(30), default="", index=True)
    name: Mapped[str] = mapped_column(String(220))
    category: Mapped[str] = mapped_column(String(100), default="Outros")
    unit: Mapped[str] = mapped_column(String(20), default="UN")
    minimum_stock: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)

class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    product_id: Mapped[int] = mapped_column(index=True)
    movement_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    document: Mapped[str] = mapped_column(String(100), default="")
    movement_date: Mapped[date] = mapped_column(Date, default=date.today)

class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    number: Mapped[str] = mapped_column(String(40))
    customer_name: Mapped[str] = mapped_column(String(220), default="Consumidor final")
    customer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_document: Mapped[str] = mapped_column(String(20), default="")
    customer_person_type: Mapped[str] = mapped_column(String(2), default="PF")
    customer_state_registration: Mapped[str] = mapped_column(String(30), default="")
    payment_method: Mapped[str] = mapped_column(String(40), default="PIX")
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    customer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_document: Mapped[str] = mapped_column(String(20), default="")
    customer_person_type: Mapped[str] = mapped_column(String(2), default="PF")
    payment_method: Mapped[str] = mapped_column(String(40), default="PIX")
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    sale_date: Mapped[date] = mapped_column(Date, default=date.today)
    status: Mapped[str] = mapped_column(String(30), default="CONCLUÍDA")

class Payable(Base):
    __tablename__ = "payables"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    supplier: Mapped[str] = mapped_column(String(220))
    due_date: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(30), default="EM ABERTO")


class SaleItem(Base):
    __tablename__ = "sale_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)
    product_id: Mapped[int] = mapped_column(index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class Receivable(Base):
    __tablename__ = "receivables"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    customer: Mapped[str] = mapped_column(String(220), default="Consumidor final")
    description: Mapped[str] = mapped_column(String(220), default="")
    due_date: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(30), default="EM ABERTO")
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sale_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ImportedPdf(Base):
    __tablename__ = "imported_pdfs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    document_type: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    supplier: Mapped[str] = mapped_column(String(220), default="")
    supplier_document: Mapped[str] = mapped_column(String(30), default="")
    document_number: Mapped[str] = mapped_column(String(80), default="")
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    barcode: Mapped[str] = mapped_column(String(120), default="")
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FiscalConfig(Base):
    __tablename__ = "fiscal_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True, unique=True)
    provider: Mapped[str] = mapped_column(String(60), default="NUVEM_FISCAL")
    environment: Mapped[str] = mapped_column(String(20), default="HOMOLOGACAO")
    client_id_encrypted: Mapped[str] = mapped_column(Text, default="")
    client_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    certificate_path: Mapped[str] = mapped_column(String(500), default="")
    certificate_password_encrypted: Mapped[str] = mapped_column(Text, default="")
    automatic_issue: Mapped[bool] = mapped_column(Boolean, default=False)
    series: Mapped[str] = mapped_column(String(10), default="1")
    last_number: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    person_type: Mapped[str] = mapped_column(String(2), default="PF")
    name: Mapped[str] = mapped_column(String(220))
    trade_name: Mapped[str] = mapped_column(String(220), default="")
    document: Mapped[str] = mapped_column(String(20), default="", index=True)
    state_registration: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(180), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    zip_code: Mapped[str] = mapped_column(String(10), default="")
    address: Mapped[str] = mapped_column(String(220), default="")
    number: Mapped[str] = mapped_column(String(30), default="")
    complement: Mapped[str] = mapped_column(String(120), default="")
    district: Mapped[str] = mapped_column(String(120), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(2), default="SP")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    legal_name: Mapped[str] = mapped_column(String(220))
    trade_name: Mapped[str] = mapped_column(String(220), default="")
    cnpj: Mapped[str] = mapped_column(String(14), default="", index=True)
    state_registration: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(180), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ImportedNfe(Base):
    __tablename__ = "imported_nfe"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    access_key: Mapped[str] = mapped_column(String(44), unique=True, index=True)
    invoice_number: Mapped[str] = mapped_column(String(30), default="")
    series: Mapped[str] = mapped_column(String(10), default="")
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(220), default="")
    supplier_cnpj: Mapped[str] = mapped_column(String(14), default="")
    total_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE")
    filename: Mapped[str] = mapped_column(String(255), default="")
    stored_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportedNfeItem(Base):
    __tablename__ = "imported_nfe_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    nfe_id: Mapped[int] = mapped_column(ForeignKey("imported_nfe.id"), index=True)
    product_code: Mapped[str] = mapped_column(String(80), default="")
    barcode: Mapped[str] = mapped_column(String(30), default="")
    description: Mapped[str] = mapped_column(String(220), default="")
    ncm: Mapped[str] = mapped_column(String(12), default="")
    cfop: Mapped[str] = mapped_column(String(10), default="")
    unit: Mapped[str] = mapped_column(String(20), default="UN")
    invoiced_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0)
    received_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0)
    total_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    matched_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NfeInstallment(Base):
    __tablename__ = "nfe_installments"
    id: Mapped[int] = mapped_column(primary_key=True)
    nfe_id: Mapped[int] = mapped_column(ForeignKey("imported_nfe.id"), index=True)
    installment_number: Mapped[str] = mapped_column(String(30), default="")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)


class FiscalDocument(Base):
    __tablename__ = "fiscal_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    sale_id: Mapped[int] = mapped_column(index=True)
    document_type: Mapped[str] = mapped_column(String(10), default="NFE")
    environment: Mapped[str] = mapped_column(String(20), default="HOMOLOGACAO")
    status: Mapped[str] = mapped_column(String(40), default="PENDENTE")
    access_key: Mapped[str] = mapped_column(String(44), default="")
    protocol: Mapped[str] = mapped_column(String(80), default="")
    xml_path: Mapped[str] = mapped_column(String(500), default="")
    danfe_path: Mapped[str] = mapped_column(String(500), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GmailImportLog(Base):
    __tablename__ = "gmail_import_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(160), default="")
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sender: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    filename: Mapped[str] = mapped_column(String(500), default="")
    access_key: Mapped[str] = mapped_column(String(44), default="", index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SefazDistributionConfig(Base):
    __tablename__="sefaz_distribution_configs"
    id: Mapped[int]=mapped_column(primary_key=True)
    tenant_id: Mapped[int]=mapped_column(index=True)
    company_id: Mapped[int]=mapped_column(index=True,unique=True)
    environment: Mapped[str]=mapped_column(String(20),default="PRODUCAO")
    last_nsu: Mapped[str]=mapped_column(String(15),default="000000000000000")
    max_nsu: Mapped[str]=mapped_column(String(15),default="000000000000000")
    automatic_import: Mapped[bool]=mapped_column(Boolean,default=False)
    last_query_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    last_status_code: Mapped[str]=mapped_column(String(10),default="")
    last_status_message: Mapped[str]=mapped_column(String(500),default="")
    updated_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class SefazDistributionDocument(Base):
    __tablename__="sefaz_distribution_documents"
    id: Mapped[int]=mapped_column(primary_key=True)
    tenant_id: Mapped[int]=mapped_column(index=True)
    company_id: Mapped[int]=mapped_column(index=True)
    nsu: Mapped[str]=mapped_column(String(15),default="",index=True)
    schema_name: Mapped[str]=mapped_column(String(100),default="")
    access_key: Mapped[str]=mapped_column(String(44),default="",index=True)
    document_type: Mapped[str]=mapped_column(String(40),default="")
    issuer_name: Mapped[str]=mapped_column(String(220),default="")
    issuer_document: Mapped[str]=mapped_column(String(20),default="")
    issue_date: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    total_value: Mapped[Decimal]=mapped_column(Numeric(14,2),default=0)
    xml_path: Mapped[str]=mapped_column(String(500),default="")
    status: Mapped[str]=mapped_column(String(30),default="RECEBIDO")
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class ShopeeShop(Base):
    __tablename__ = "shopee_shops"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    shop_id: Mapped[int] = mapped_column(Integer, index=True)
    shop_name: Mapped[str] = mapped_column(String(220), default="")
    region: Mapped[str] = mapped_column(String(10), default="BR")
    access_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShopeeOrder(Base):
    __tablename__ = "shopee_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    shopee_shop_id: Mapped[int] = mapped_column(ForeignKey("shopee_shops.id"), index=True)
    order_sn: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(60), default="")
    buyer_username: Mapped[str] = mapped_column(String(180), default="")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(10), default="BRL")
    tracking_number: Mapped[str] = mapped_column(String(120), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    order_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShopeeSyncLog(Base):
    __tablename__ = "shopee_sync_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    shopee_shop_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="OK")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class DdaConnector(Base):
    __tablename__ = "dda_connectors"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(140), default="")
    environment: Mapped[str] = mapped_column(String(20), default="PRODUCAO")
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_create_payable: Mapped[bool] = mapped_column(Boolean, default=True)
    require_invoice_match: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(30), default="AGUARDANDO_CREDENCIAIS")
    last_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DdaBoleto(Base):
    __tablename__ = "dda_boletos"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("dda_connectors.id"), nullable=True, index=True
    )
    external_id: Mapped[str] = mapped_column(String(180), default="", index=True)
    digitable_line: Mapped[str] = mapped_column(String(100), default="", index=True)
    beneficiary_name: Mapped[str] = mapped_column(String(220), default="")
    beneficiary_document: Mapped[str] = mapped_column(String(20), default="", index=True)
    payer_document: Mapped[str] = mapped_column(String(20), default="")
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="NOVO", index=True)
    bank_status: Mapped[str] = mapped_column(String(50), default="")
    invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    payable_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    match_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[str] = mapped_column(String(20), default="MEDIO")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DdaSyncLog(Base):
    __tablename__ = "dda_sync_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    connector_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(30), default="OK")
    imported: Mapped[int] = mapped_column(Integer, default=0)
    duplicated: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class BankStatementImport(Base):
    __tablename__ = "bank_statement_imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    bank_name: Mapped[str] = mapped_column(String(120), default="")
    account_name: Mapped[str] = mapped_column(String(160), default="")
    file_name: Mapped[str] = mapped_column(String(240), default="")
    file_hash: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    format: Mapped[str] = mapped_column(String(20), default="")
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    statement_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_statement_imports.id"), nullable=True, index=True
    )
    external_id: Mapped[str] = mapped_column(String(180), default="", index=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    document_number: Mapped[str] = mapped_column(String(120), default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    transaction_type: Mapped[str] = mapped_column(String(20), default="DEBITO")
    category: Mapped[str] = mapped_column(String(100), default="")
    counterparty_name: Mapped[str] = mapped_column(String(220), default="")
    counterparty_document: Mapped[str] = mapped_column(String(20), default="", index=True)
    payable_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    receivable_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dda_boleto_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reconciliation_status: Mapped[str] = mapped_column(
        String(30), default="PENDENTE", index=True
    )
    match_score: Mapped[int] = mapped_column(Integer, default=0)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BankReconciliationLog(Base):
    __tablename__ = "bank_reconciliation_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="OK")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PagBankConfig(Base):
    __tablename__ = "pagbank_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True, unique=True)
    environment: Mapped[str] = mapped_column(String(20), default="SANDBOX")
    token_encrypted: Mapped[str] = mapped_column(Text, default="")
    webhook_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(30), default="NAO_CONFIGURADO")
    last_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PagBankPayment(Base):
    __tablename__ = "pagbank_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    company_id: Mapped[int] = mapped_column(index=True)
    receivable_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reference_id: Mapped[str] = mapped_column(String(120), index=True)
    order_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    charge_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    payment_type: Mapped[str] = mapped_column(String(30), default="PIX")
    customer_name: Mapped[str] = mapped_column(String(220), default="")
    customer_email: Mapped[str] = mapped_column(String(220), default="")
    customer_tax_id: Mapped[str] = mapped_column(String(20), default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(40), default="WAITING")
    qr_code_text: Mapped[str] = mapped_column(Text, default="")
    qr_code_link: Mapped[str] = mapped_column(Text, default="")
    boleto_barcode: Mapped[str] = mapped_column(String(100), default="")
    boleto_pdf: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PagBankWebhookLog(Base):
    __tablename__ = "pagbank_webhook_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    order_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    event_type: Mapped[str] = mapped_column(String(80), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="")
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
