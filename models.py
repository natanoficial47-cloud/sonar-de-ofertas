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
