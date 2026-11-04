"""Modelo de dominio das ofertas."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Offer:
    """Uma oferta vinda de uma loja.

    `is_demo` marca ofertas geradas localmente (mock). Esse tipo de oferta
    nunca aparece no grupo sem o rotulo de demonstracao, para nao confundir
    produto real com produto de teste.
    """

    store: str
    external_id: str
    title: str
    url: str
    current_price: float
    original_price: float | None = None
    discount_pct: float = 0.0
    image_url: str | None = None
    coupon: str | None = None
    is_demo: bool = False
    # Maior preco entre as ofertas do mesmo produto de catalogo. Quando o
    # anuncio nao tem preco de tabela, isso mostra que ele e o mais barato
    # entre N vendedores do mesmo produto.
    comparison_price: float | None = None
    comparison_count: int = 0
    # O preco de tabela do anuncio tambem aparece no preco de outros
    # vendedores do mesmo produto (corroboracao de mercado).
    table_corroborated: bool = False
    # Frete: quando o anuncio tem frete gratis, o preco anunciado e o que a
    # pagina do Mercado Livre mostra em destaque.
    free_shipping: bool = False
    shipping_cost: float | None = None
    # Termo de busca que trouxe o produto ("cadeira gamer", "air fryer"...).
    # Serve para rotular a oferta no grupo e para garantir variedade: o
    # grupo precisa misturar nichos, nao so o que tem o maior desconto.
    keyword: str | None = None

    @property
    def url_hash(self) -> str:
        """Hash estavel da URL, usado como chave de anti-duplicidade."""
        normalized = self.url.strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def effective_discount(self) -> float:
        """Desconto real: usa o percentual informado ou calcula pelo preco."""
        if self.discount_pct > 0:
            return round(self.discount_pct, 2)
        if self.original_price and self.original_price > self.current_price:
            pct = (1 - self.current_price / self.original_price) * 100
            return round(pct, 2)
        return 0.0

    @property
    def effective_comparison_pct(self) -> float:
        """Quanto o anuncio e mais barato que as outras ofertas do produto."""
        if (
            self.comparison_price
            and self.comparison_price > self.current_price
        ):
            pct = (1 - self.current_price / self.comparison_price) * 100
            return round(pct, 2)
        return 0.0

    @property
    def attractiveness(self) -> float:
        """Maior desconto entre as duas origens (usado para ordenar)."""
        return max(self.effective_discount, self.effective_comparison_pct)

    def to_row(self) -> dict:
        return {
            "store": self.store,
            "external_id": self.external_id,
            "url_hash": self.url_hash,
            "title": self.title,
            "url": self.url,
            "current_price": float(self.current_price),
            "original_price": float(self.original_price) if self.original_price else None,
            "discount_pct": self.effective_discount,
            "is_demo": self.is_demo,
        }
