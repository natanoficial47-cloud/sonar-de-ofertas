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

    discount = offer.effective_discount
    comparison = offer.effective_comparison_pct

    if discount > 0:
        lines.append(f"\U0001f4b0   De: {brl(offer.original_price)}")
        lines.append(
            f"⚡   <b>Por: {brl(offer.current_price)}</b> <i>({discount:.0f}% OFF)</i>"
        )
        nota = "Desconto sobre o preço de tabela do anúncio"
        if offer.table_corroborated:
            nota += " (mesmo preço em outros vendedores do produto)"
        lines.append(f"   <i>{nota}</i>")
    else:
        if comparison > 0:
            extra = " com frete grátis" if offer.free_shipping else ""
            lines.append(
                f"\U0001f4d7   <b>Menor preço entre {offer.comparison_count} ofertas "
                f"do produto{extra}</b> (preço típico {brl(offer.comparison_price)})"
            )
            lines.append(
                f"   <i>Economia de {comparison:.0f}% frente ao preço típico "
                "do produto</i>"
            )
        lines.append(f"⚡   <b>Por: {brl(offer.current_price)}</b>")

    lines.append(f"\U0001f6d2   <i>Loja:</i> {escape_text(offer.store)}")

    if offer.keyword:
        # O rotulo e o termo de busca usado, nao a categoria oficial do
        # produto: a API nao devolve o nome da categoria. "Busca" nao mente.
        lines.append(f"\U0001f3f7   <i>Busca:</i> {escape_text(offer.keyword)}")

    if offer.free_shipping:
        lines.append("\U0001f69a   <i>Frete: grátis</i>")
    elif offer.shipping_cost:
        lines.append(f"\U0001f69a   <i>Frete: {brl(offer.shipping_cost)}</i>")

    if offer.coupon:
        lines.append(f"\U0001f3e6   <i>Cupom:</i> {escape_text(offer.coupon)}")

    lines.append("")
    lines.append(f"\U0001f449   <a href=\"{url}\">CLIQUE AQUI PARA COMPRAR</a>")

    return "\n".join(lines)


def build_cycle_header(count: int) -> str:
    return f"\U0001f4c3 <b>{count} achadinho(s) encontrado(s) neste ciclo</b>"
