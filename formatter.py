"""Formatacao das mensagens do Telegram (HTML, com escape dos campos dinamicos)."""

from __future__ import annotations

import html

from models import Offer


def brl(value: float | None) -> str:
    if value is None:
        return "R$ --"
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def escape_text(value: str) -> str:
    return html.escape(value or "", quote=False)


def escape_url(value: str) -> str:
    return html.escape(value or "", quote=True)


def build_message(offer: Offer) -> str:
    """Monta a mensagem de uma oferta.

    Todo conteudo dinamico (titulo, preco, link) passa por escape: titulos de
    produto vem do ecommerce e podem conter &, < e >, que quebrariam o
    parse_mode=HTML e o Telegram recusaria a mensagem inteira.
    """
    title = escape_text(offer.title)
    url = escape_url(offer.url)

    lines: list[str] = []
    if offer.is_demo:
        lines.append("<b>DEMO - oferta de teste, nao e produto real</b>")
        lines.append("")
    lines.append("\U0001f525   <b>ACHADINHO AUTOMÁTICO</b>   \U0001f525")
    lines.append("")
    lines.append(f"\U0001f4e6   <b>{title}</b>")

    if offer.original_price and offer.original_price > offer.current_price:
        lines.append(f"\U0001f4b0   De: ~{brl(offer.original_price)}~")

    discount = f" ({offer.effective_discount:.0f}% OFF)" if offer.effective_discount else ""
    lines.append(
        f"⚡   <b>Por: {brl(offer.current_price)}</b> <i>{discount.strip()}</i>".replace(
            "<i></i>", ""
        )
    )
    lines.append(f"\U0001f6d2   <i>Loja:</i> {escape_text(offer.store)}")

    if offer.coupon:
        lines.append(f"\U0001f3e6   <i>Cupom:</i> {escape_text(offer.coupon)}")

    lines.append("")
    lines.append(f"\U0001f449   <a href=\"{url}\">CLIQUE AQUI PARA COMPRAR</a>")

    return "\n".join(lines)


def build_cycle_header(count: int) -> str:
    return f"\U0001f4c3 <b>{count} achadinho(s) encontrado(s) neste ciclo</b>"
