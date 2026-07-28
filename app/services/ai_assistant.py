from __future__ import annotations

import os
import re
from datetime import date
from decimal import Decimal
from typing import Any

from openai import OpenAI
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.models import Payable, Product, Receivable, Sale, SaleItem, StockMovement


def _money(value: Any) -> str:
    amount = Decimal(str(value or 0))
    text = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def _stock_expression():
    return func.coalesce(
        func.sum(
            case(
                (StockMovement.movement_type == "ENTRADA", StockMovement.quantity),
                else_=-StockMovement.quantity,
            )
        ),
        0,
    )


def build_company_context(db: Session, tenant_id: int, company_id: int, question: str) -> dict[str, Any]:
    today = date.today()
    month_start = today.replace(day=1)

    sales_today = db.query(func.coalesce(func.sum(Sale.total), 0), func.count(Sale.id)).filter(
        Sale.tenant_id == tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date == today,
    ).one()
    sales_month = db.query(func.coalesce(func.sum(Sale.total), 0), func.count(Sale.id)).filter(
        Sale.tenant_id == tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= month_start,
        Sale.sale_date <= today,
    ).one()
    receivable_open = db.query(func.coalesce(func.sum(Receivable.value), 0)).filter(
        Receivable.tenant_id == tenant_id,
        Receivable.company_id == company_id,
        Receivable.status != "RECEBIDO",
    ).scalar()
    payable_open = db.query(func.coalesce(func.sum(Payable.value), 0)).filter(
        Payable.tenant_id == tenant_id,
        Payable.company_id == company_id,
        Payable.status != "PAGO",
    ).scalar()
    overdue_payables = db.query(func.coalesce(func.sum(Payable.value), 0), func.count(Payable.id)).filter(
        Payable.tenant_id == tenant_id,
        Payable.company_id == company_id,
        Payable.status != "PAGO",
        Payable.due_date < today,
    ).one()

    stock_rows = db.query(
        Product.id,
        Product.name,
        Product.sku,
        Product.barcode,
        Product.minimum_stock,
        Product.unit_cost,
        _stock_expression().label("stock"),
    ).outerjoin(
        StockMovement,
        (StockMovement.product_id == Product.id)
        & (StockMovement.tenant_id == tenant_id)
        & (StockMovement.company_id == company_id),
    ).filter(
        Product.tenant_id == tenant_id,
        Product.company_id == company_id,
    ).group_by(Product.id).all()

    total_stock_value = sum(Decimal(str(row.stock or 0)) * Decimal(str(row.unit_cost or 0)) for row in stock_rows)
    low_stock = [row for row in stock_rows if int(row.stock or 0) <= int(row.minimum_stock or 0)]

    words = [word for word in re.findall(r"[\wÀ-ÿ-]+", question.lower()) if len(word) >= 3]
    matched_products = []
    if words:
        filters = []
        for word in words[:8]:
            like = f"%{word}%"
            filters.extend([Product.name.ilike(like), Product.sku.ilike(like), Product.barcode.ilike(like)])
        matched_products = db.query(
            Product.name,
            Product.sku,
            Product.barcode,
            Product.sale_price,
            _stock_expression().label("stock"),
        ).outerjoin(
            StockMovement,
            (StockMovement.product_id == Product.id)
            & (StockMovement.tenant_id == tenant_id)
            & (StockMovement.company_id == company_id),
        ).filter(
            Product.tenant_id == tenant_id,
            Product.company_id == company_id,
            or_(*filters),
        ).group_by(Product.id).limit(20).all()

    top_products = db.query(
        Product.name,
        func.coalesce(func.sum(SaleItem.quantity), 0).label("quantity"),
        func.coalesce(func.sum(SaleItem.total), 0).label("total"),
    ).join(SaleItem, SaleItem.product_id == Product.id).join(Sale, Sale.id == SaleItem.sale_id).filter(
        Sale.tenant_id == tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= month_start,
        Sale.sale_date <= today,
    ).group_by(Product.id).order_by(func.sum(SaleItem.quantity).desc()).limit(10).all()

    return {
        "data_referencia": today.isoformat(),
        "vendas_hoje": {"valor": _money(sales_today[0]), "quantidade": int(sales_today[1] or 0)},
        "vendas_mes": {"valor": _money(sales_month[0]), "quantidade": int(sales_month[1] or 0)},
        "financeiro": {
            "a_receber_aberto": _money(receivable_open),
            "a_pagar_aberto": _money(payable_open),
            "contas_vencidas": int(overdue_payables[1] or 0),
            "valor_vencido": _money(overdue_payables[0]),
        },
        "estoque": {
            "produtos": len(stock_rows),
            "valor_total": _money(total_stock_value),
            "estoque_baixo": len(low_stock),
            "itens_estoque_baixo": [
                {"produto": row.name, "saldo": int(row.stock or 0), "minimo": int(row.minimum_stock or 0)}
                for row in low_stock[:20]
            ],
        },
        "produtos_encontrados": [
            {"produto": row.name, "sku": row.sku, "codigo_barras": row.barcode, "saldo": int(row.stock or 0), "preco": _money(row.sale_price)}
            for row in matched_products
        ],
        "mais_vendidos_mes": [
            {"produto": row.name, "quantidade": int(row.quantity or 0), "valor": _money(row.total)}
            for row in top_products
        ],
    }


def answer_question(context: dict[str, Any], question: str, history: list[dict[str, str]] | None = None) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("A chave OPENAI_API_KEY ainda não foi configurada no Railway.")

    model = os.getenv("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    client = OpenAI(api_key=api_key, timeout=40.0)
    recent = (history or [])[-8:]
    transcript = "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in recent)

    response = client.responses.create(
        model=model,
        instructions=(
            "Você é o Gestão IA, assistente interno de um ERP brasileiro. "
            "Responda em português do Brasil, de forma objetiva e amigável. "
            "Use somente os dados fornecidos no contexto. Não invente números. "
            "Esta versão é somente leitura: nunca diga que alterou, cadastrou, apagou ou pagou algo. "
            "Quando faltarem dados, explique claramente. Valores devem permanecer em reais (R$)."
        ),
        input=f"CONTEXTO DO ERP:\n{context}\n\nHISTÓRICO RECENTE:\n{transcript}\n\nPERGUNTA:\n{question}",
    )
    return response.output_text.strip()
