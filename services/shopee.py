"""Shopee Open Platform (API oficial de parceiro/afiliado).

A assinatura e HMAC-SHA256 do caminho + parametros ordenados, com
chave base64. Sem partner_id/partner_key o adaptador fica em modo demo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from config import ShopeeSettings
from models import Offer
from services.base import StoreAdapter

log = logging.getLogger(__name__)


def sign_request(partner_key: str, path: str, params: dict[str, Any]) -> str:
    """Assinatura oficial da Shopee.

    A string assinada concatena o caminho com cada chave e valor (em ordem
    alfabetica, escapados), ignorando `partner_id` e `sign`.
    """
    payload = path
    for key in sorted(params):
        if key in ("partner_id", "sign"):
            continue
        payload += quote(str(key), safe="")
        payload += quote(str(params[key]), safe="")
    digest = hmac.new(
        partner_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80]


class ShopeeAdapter(StoreAdapter):
    name = "Shopee"

    def __init__(self, settings: ShopeeSettings):
        super().__init__(settings)
        self.cfg = settings

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    async def fetch(self, client: Any) -> list[Offer]:
        path = "/api/v2/product/get_items"
        offers: list[Offer] = []
        seen: set[str] = set()

        for keyword in self.cfg.keywords:
            params: dict[str, Any] = {
                "partner_id": self.cfg.partner_id,
                "page": 1,
                "page_size": 50,
                "timestamp": int(time.time()),
                "keyword": keyword,
            }
            params["sign"] = sign_request(self.cfg.partner_key, path, params)

            url = f"https://{self.cfg.host}{path}"
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("items", []) or []:
                offer = self._parse_item(item)
                if offer is None or offer.url_hash in seen:
                    continue
                seen.add(offer.url_hash)
                offers.append(offer)

        return offers

    def _parse_item(self, item: dict) -> Offer | None:
        item_id = item.get("itemid") or item.get("item_id")
        name = item.get("name")
        if not item_id or not name:
            return None

        # A Shopee devolve valores em centavos.
        price_min = item.get("price_min")
        if price_min is None:
            return None
        current = float(price_min) / 100.0

        normal = item.get("normal_price")
        original = float(normal) / 100.0 if normal else None

        discount = item.get("discount_percentage")
        if not discount and original and original > current:
            discount = (1 - current / original) * 100

        shop_id = self.cfg.shop_id or item.get("shopid")
        if shop_id:
            url = f"https://shopee.com.br/{shop_id}/{_slugify(str(name))}-{item_id}"
        else:
            url = f"https://shopee.com.br/product/{item_id}"

        return Offer(
            store=self.name,
            external_id=str(item_id),
            title=str(name),
            url=url,
            current_price=round(current, 2),
            original_price=round(original, 2) if original else None,
            discount_pct=round(float(discount), 2) if discount else 0.0,
            image_url=str(item.get("image")) if item.get("image") else None,
        )
