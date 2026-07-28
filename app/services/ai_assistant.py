
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal
from typing import Any

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

    sales_today = db.query(
        func.coalesce(func.sum(Sale.total), 0),
        func.count(Sale.id),
    ).filter(
        Sale.tenant_id == tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date == today,
    ).one()

    sales_month = db.query(
        func.coalesce(func.sum(Sale.total), 0),
        func.count(Sale.id),
    ).filter(
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

    overdue_payables = db.query(
        func.coalesce(func.sum(Payable.value), 0),
        func.count(Payable.id),
    ).filter(
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
    ).group_by(Product.id).all()

    total_stock_value = sum(
        Decimal(str(row.stock or 0)) * Decimal(str(row.unit_cost or 0))
        for row in stock_rows
    )
    total_items = sum(max(int(row.stock or 0), 0) for row in stock_rows)
    low_stock = [
        row for row in stock_rows
        if int(row.stock or 0) <= int(row.minimum_stock or 0)
    ]

    words = [
        word for word in re.findall(r"[\wÀ-ÿ-]+", question.lower())
        if len(word) >= 3
    ]
    stop_words = {
        "quanto", "tenho", "produto", "produtos", "estoque", "qual", "quais",
        "vendi", "vendas", "hoje", "mes", "mês", "valor", "preco", "preço",
        "procure", "buscar", "busque", "mostre", "lista", "listar", "meu",
        "minha", "com", "para", "mais", "menos", "esta", "está", "estao", "estão",
    }
    search_words = [w for w in words if w not in stop_words]

    matched_products = []
    if search_words:
        filters = []
        for word in search_words[:8]:
            like = f"%{word}%"
            filters.extend([
                Product.name.ilike(like),
                Product.sku.ilike(like),
                Product.barcode.ilike(like),
            ])
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
    ).join(
        SaleItem, SaleItem.product_id == Product.id
    ).join(
        Sale, Sale.id == SaleItem.sale_id
    ).filter(
        Sale.tenant_id == tenant_id,
        Sale.company_id == company_id,
        Sale.sale_date >= month_start,
        Sale.sale_date <= today,
    ).group_by(Product.id).order_by(
        func.sum(SaleItem.quantity).desc()
    ).limit(10).all()

    return {
        "vendas_hoje": {
            "valor": _money(sales_today[0]),
            "quantidade": int(sales_today[1] or 0),
        },
        "vendas_mes": {
            "valor": _money(sales_month[0]),
            "quantidade": int(sales_month[1] or 0),
        },
        "financeiro": {
            "a_receber_aberto": _money(receivable_open),
            "a_pagar_aberto": _money(payable_open),
            "contas_vencidas": int(overdue_payables[1] or 0),
            "valor_vencido": _money(overdue_payables[0]),
        },
        "estoque": {
            "produtos": len(stock_rows),
            "itens": total_items,
            "valor_total": _money(total_stock_value),
            "estoque_baixo": len(low_stock),
            "itens_estoque_baixo": [
                {
                    "produto": row.name,
                    "saldo": int(row.stock or 0),
                    "minimo": int(row.minimum_stock or 0),
                }
                for row in low_stock[:20]
            ],
        },
        "produtos_encontrados": [
            {
                "produto": row.name,
                "sku": row.sku or "",
                "saldo": int(row.stock or 0),
                "preco": _money(row.sale_price),
            }
            for row in matched_products
        ],
        "mais_vendidos_mes": [
            {
                "produto": row.name,
                "quantidade": int(row.quantity or 0),
                "valor": _money(row.total),
            }
            for row in top_products
        ],
    }


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))


def answer_question(context: dict[str, Any], question: str, history=None) -> str:
    q = _normalize(question).strip()
    sales_today = context["vendas_hoje"]
    sales_month = context["vendas_mes"]
    finance = context["financeiro"]
    stock = context["estoque"]
    products = context["produtos_encontrados"]
    top = context["mais_vendidos_mes"]

    if _contains_any(q, ["oi", "ola", "bom dia", "boa tarde", "boa noite", "ajuda"]):
        return (
            "Olá! Sou o Gestão IA Local. Posso consultar vendas, estoque, produtos "
            "e financeiro sem usar API paga."
        )

    if _contains_any(q, ["vendas de hoje", "vendi hoje", "faturei hoje", "faturamento hoje"]):
        return f"Hoje foram {sales_today['quantidade']} venda(s), totalizando {sales_today['valor']}."

    if _contains_any(q, ["vendas do mes", "vendi no mes", "faturei no mes", "faturamento do mes"]):
        return f"Neste mês foram {sales_month['quantidade']} venda(s), totalizando {sales_month['valor']}."

    if _contains_any(q, ["contas vencidas", "pagar vencido", "atrasadas", "vencidas"]):
        return (
            f"Existem {finance['contas_vencidas']} conta(s) vencida(s), "
            f"somando {finance['valor_vencido']}."
        )

    if _contains_any(q, ["quanto tenho para receber", "a receber", "receber aberto"]):
        return f"O total em contas a receber em aberto é {finance['a_receber_aberto']}."

    if _contains_any(q, ["quanto tenho para pagar", "a pagar", "pagar aberto"]):
        return f"O total em contas a pagar em aberto é {finance['a_pagar_aberto']}."

    if _contains_any(q, ["estoque baixo", "produtos acabando", "repor", "reposicao"]):
        rows = stock["itens_estoque_baixo"]
        if not rows:
            return "Não há produtos com estoque igual ou abaixo do mínimo."
        lines = [
            f"{row['produto']} — saldo {row['saldo']} / mínimo {row['minimo']}"
            for row in rows[:15]
        ]
        return f"Encontrei {stock['estoque_baixo']} produto(s) com estoque baixo:\n" + _numbered(lines)

    if _contains_any(q, ["valor do estoque", "estoque total", "quantos itens", "quantos produtos"]):
        return (
            f"O estoque possui {stock['produtos']} produto(s), {stock['itens']} item(ns) "
            f"e valor de custo estimado em {stock['valor_total']}."
        )

    if _contains_any(q, ["mais vendidos", "produto mais vendido", "top produtos"]):
        if not top:
            return "Ainda não há vendas registradas neste mês para calcular os mais vendidos."
        lines = [
            f"{row['produto']} — {row['quantidade']} un. — {row['valor']}"
            for row in top[:10]
        ]
        return "Produtos mais vendidos neste mês:\n" + _numbered(lines)

    if products:
        lines = [
            f"{row['produto']} — saldo {row['saldo']} — preço {row['preco']}"
            for row in products[:15]
        ]
        return f"Encontrei {len(products)} produto(s):\n" + _numbered(lines)

    if _contains_any(q, ["resumo", "situacao", "painel", "como esta"]):
        return (
            "Resumo da empresa:\n"
            f"• Vendas hoje: {sales_today['valor']} ({sales_today['quantidade']} venda(s))\n"
            f"• Vendas no mês: {sales_month['valor']} ({sales_month['quantidade']} venda(s))\n"
            f"• A receber: {finance['a_receber_aberto']}\n"
            f"• A pagar: {finance['a_pagar_aberto']}\n"
            f"• Estoque: {stock['itens']} item(ns), valor {stock['valor_total']}\n"
            f"• Estoque baixo: {stock['estoque_baixo']} produto(s)"
        )

    return (
        "Ainda não entendi. Posso responder sobre vendas, contas, estoque baixo, "
        "valor do estoque, produtos mais vendidos e pesquisa de produtos."
    )
