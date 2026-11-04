"""Mercado Livre.

A API do Mercado Livre passou a exigir token: chamadas sem autenticacao
recebem 403 do PolicyAgent. Com `MERCADOLIVRE_ACCESS_TOKEN` a busca em
`/sites/{SITE}/search` funciona normalmente; sem token o adaptador fica em
modo demo. Quando existe `MERCADOLIVRE_AFFILIATE_ID`, a tag de afiliado e
acrescentada no link.
"""

from __future__ import annotations

import logging
from typing import Any

from config import MercadoLivreSettings
from models import Offer
from services.base import StoreAdapter

log = logging.getLogger(__name__)

API_BASE = "https://api.mercadolibre.com"


class MercadoLivreAdapter(StoreAdapter):
    name = "Mercado Livre"

    def __init__(self, settings: MercadoLivreSettings):
        super().__init__(settings)
        self.cfg = settings

    @property
    def configured(self) -> bool:
        # Sem token a API responde 403, entao a loja so entra em modo real
        # quando ha credencial.
        return bool(self.cfg.access_token)

    async def fetch(self, client: Any) -> list[Offer]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "bot-ofertas-telegram/1.0",
        }
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"

        offers: list[Offer] = []
        seen: set[str] = set()

        for keyword in self.cfg.keywords:
            url = f"{API_BASE}/sites/{self.cfg.site_id}/search"
            response = await client.get(
                url,
                params={"q": keyword, "limit": 50},
                headers=headers,
            )
            if response.status_code in (401, 403):
                log.warning(
                    "[%s] Busca recusada (%s). Verifique o token ou as keywords.",
                    self.name,
                    response.status_code,
                )
                continue
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("results", []) or []:
                offer = self._parse_item(item)
                if offer is None or offer.url_hash in seen:
                    continue
                seen.add(offer.url_hash)
                offers.append(offer)

        return offers

    def _parse_item(self, item: dict) -> Offer | None:
        item_id = item.get("id")
        title = item.get("title")
        price = item.get("price")
        if not item_id or not title or price is None:
            return None

        permalink = item.get("permalink") or f"{API_BASE}/items/{item_id}"
        if self.cfg.affiliate_id and "tag=" not in permalink:
            separator = "&" if "?" in permalink else "?"
            permalink = f"{permalink}{separator}tag={self.cfg.affiliate_id}"

        original = item.get("original_price")
        discount = item.get("discount_percentage")
        if not discount and original and float(original) > float(price):
            discount = (1 - float(price) / float(original)) * 100

        return Offer(
            store=self.name,
            external_id=str(item_id),
            title=str(title),
            url=str(permalink),
            current_price=round(float(price), 2),
            original_price=round(float(original), 2) if original else None,
            discount_pct=round(float(discount), 2) if discount else 0.0,
            image_url=str(item.get("thumbnail")) if item.get("thumbnail") else None,
        )
