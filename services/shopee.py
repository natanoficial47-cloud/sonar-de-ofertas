"""Shopee Open Platform (API oficial de parceiro/afiliado).

Assinatura (spec oficial v2):
    base = partner_id + api_path + timestamp + access_token + shop_id
    sign = hex(HMAC-SHA256(base, partner_key))   # hex minusculo, nao base64

O `sign` vai como query param, junto de partner_id/timestamp. Os demais
parametros de filtro (keyword, page...) **nao** entram na assinatura.

Endpoints de produto sao shop-level: exigem access_token (validade de ~4h) e
shop_id. O access_token e renovavel via refresh_token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from typing import Any

import httpx

from config import ShopeeSettings
from models import Offer
from services.base import StoreAdapter

log = logging.getLogger(__name__)


def sign_request(
    partner_id: str | int,
    partner_key: str,
    path: str,
    timestamp: int | str,
    access_token: str = "",
    shop_id: str | int = "",
) -> str:
    """Assinatura oficial da Shopee (HMAC-SHA256 em hexadecimal)."""
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    digest = hmac.new(
        partner_key.encode("utf-8"), base.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest


def refresh_access_token(
    client: Any,
    partner_id: str | int,
    partner_key: str,
    shop_id: str | int,
    refresh_token: str,
) -> str:
    """Renova o access_token da loja. Retorna o novo token.

    O access_token da Shopee expira em ~4h; o cron roda a cada 2h, entao a
    renovacao precisa acontecer sozinha.
    """
    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    sign = sign_request(partner_id, partner_key, path, timestamp)
    url = f"https://partner.shopeebr.com{path}"
    params = {
        "partner_id": partner_id,
        "timestamp": timestamp,
        "sign": sign,
        "shop_id": shop_id,
        "refresh_token": refresh_token,
    }

    response = httpx.post(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"Renovacao do token da Shopee falhou: {payload}")
    return token


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80]


class ShopeeAdapter(StoreAdapter):
    name = "Shopee"
    key = "shopee"

    def __init__(self, settings: ShopeeSettings):
        super().__init__(settings)
        self.cfg = settings
        # Token renovado durante o ciclo (o main persiste no banco).
        self.refreshed_access_token: str | None = None

    @property
    def configured(self) -> bool:
        return all(
            [
                self.cfg.partner_id,
                self.cfg.partner_key,
                self.cfg.shop_id,
                self.cfg.access_token,
            ]
        )

    async def ping(self, client: Any) -> str:
        """Um get_items com page_size=1 prova que o token e a assinatura valem."""
        path = "/api/v2/product/get_items"
        timestamp = int(time.time())
        params = {
            "partner_id": self.cfg.partner_id,
            "timestamp": timestamp,
            "access_token": self.cfg.access_token,
            "shop_id": self.cfg.shop_id,
            "sign": sign_request(
                self.cfg.partner_id,
                self.cfg.partner_key,
                path,
                timestamp,
                self.cfg.access_token,
                self.cfg.shop_id,
            ),
            "keyword": self.cfg.keywords[0] if self.cfg.keywords else "fone",
            "page": 1,
            "page_size": 1,
        }
        url = f"https://{self.cfg.host}{path}"
        response = await client.get(url, params=params)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"HTTP {response.status_code}: token expirado/invalido "
                "(falta SHOPEE_REFRESH_TOKEN?)"
            )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:150]}")
        return "token e assinatura validos, get_items respondendo"

    async def fetch(self, client: Any) -> list[Offer]:
        path = "/api/v2/product/get_items"
        offers: list[Offer] = []
        seen: set[str] = set()
        access_token = self.cfg.access_token

        for keyword in self.cfg.keywords:
            timestamp = int(time.time())
            sign = sign_request(
                self.cfg.partner_id,
                self.cfg.partner_key,
                path,
                timestamp,
                access_token,
                self.cfg.shop_id,
            )
            params = {
                "partner_id": self.cfg.partner_id,
                "timestamp": timestamp,
                "access_token": access_token,
                "shop_id": self.cfg.shop_id,
                "sign": sign,
                "keyword": keyword,
                "page": 1,
                "page_size": 50,
                "newest": 0,
                "order": "desc",
            }

            url = f"https://{self.cfg.host}{path}"
            response = await client.get(url, params=params)

            if response.status_code in (401, 403):
                renewed = await self._try_refresh(client)
                if renewed:
                    access_token = renewed
                    timestamp = int(time.time())
                    params["access_token"] = access_token
                    params["timestamp"] = timestamp
                    params["sign"] = sign_request(
                        self.cfg.partner_id,
                        self.cfg.partner_key,
                        path,
                        timestamp,
                        access_token,
                        self.cfg.shop_id,
                    )
                    response = await client.get(url, params=params)

            if response.status_code >= 400:
                log.error(
                    "[Shopee] HTTP %s em %s: %s",
                    response.status_code,
                    path,
                    response.text[:200],
                )
                continue

            payload = response.json()

            if payload.get("error"):
                log.error("[Shopee] Erro da API: %s", payload.get("error"))
                continue

            for item in payload.get("items", []) or []:
                offer = self._parse_item(item)
                if offer is None or offer.url_hash in seen:
                    continue
                seen.add(offer.url_hash)
                offers.append(offer)

        return offers

    async def _try_refresh(self, client: Any) -> str | None:
        """Renova o access_token expirado. Retorna o novo token, se conseguir."""
        if not self.cfg.refresh_token:
            log.error(
                "[Shopee] Token expirado/invalido e nao ha SHOPEE_REFRESH_TOKEN "
                "para renovar. Defina a variavel de ambiente."
            )
            return None
        try:
            token = await asyncio.to_thread(
                refresh_access_token,
                client,
                self.cfg.partner_id,
                self.cfg.partner_key,
                self.cfg.shop_id,
                self.cfg.refresh_token,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("[Shopee] Falha ao renovar o access_token: %s", exc)
            return None

        log.info("[Shopee] access_token renovado")
        self.refreshed_access_token = token
        return token

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
            # Na Shopee o itemid ja e a identidade do produto.
            product_id=str(item_id),
            title=str(name),
            url=url,
            current_price=round(current, 2),
            original_price=round(original, 2) if original else None,
            discount_pct=round(float(discount), 2) if discount else 0.0,
            image_url=str(item.get("image")) if item.get("image") else None,
        )
