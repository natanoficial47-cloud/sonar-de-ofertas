"""Amazon Product Advertising API v5 (PA-API).

A assinatura e SigV4 (AWS4-HMAC-SHA256), implementada com a biblioteca
padrao para nao depender do boto3. Sem as credenciais do Associates o
adaptador fica em modo demo.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from config import AmazonSettings
from models import Offer
from services.base import StoreAdapter

log = logging.getLogger(__name__)

SERVICE = "paapi5"
TARGET = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(f"AWS4{secret_key}".encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _canonical_query(params: dict[str, Any]) -> str:
    items = sorted((str(k), str(v)) for k, v in params.items())
    return "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in items)


def build_signed_headers(
    method: str,
    path: str,
    params: dict[str, Any],
    headers: dict[str, str],
    access_key: str,
    secret_key: str,
    region: str,
    service: str = SERVICE,
    amz_date: str | None = None,
) -> dict[str, str]:
    """Monta os headers com a assinatura SigV4 (AWS4-HMAC-SHA256)."""
    amz_date = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]

    # A data precisa entrar na assinatura: por isso entra nos headers antes do
    # calculo da canonical request.
    headers = dict(headers)
    headers["X-Amz-Date"] = amz_date

    signed_header_names = sorted(k.lower() for k in headers)
    canonical_headers = "".join(
        f"{name}:{str(headers[key]).strip()}\n"
        for name, key in zip(signed_header_names, sorted(headers, key=str.lower))
    )
    signed_headers = ";".join(signed_header_names)
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_request = "\n".join(
        [
            method,
            path,
            _canonical_query(params),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    result = dict(headers)
    result["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result


def _dig(data: Any, *paths: tuple[Any, ...]) -> Any:
    """Busca o primeiro valor nao nulo em varios caminhos da resposta.

    Aceita indices inteiros no caminho (ex.: ("Offers", "Listings", 0, "Price")).
    """
    for path in paths:
        current = data
        for key in path:
            if isinstance(key, int):
                if not isinstance(current, (list, tuple)) or len(current) <= key:
                    current = None
                    break
                current = current[key]
            else:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
        if current is not None:
            return current
    return None


class AmazonAdapter(StoreAdapter):
    name = "Amazon"

    def __init__(self, settings: AmazonSettings):
        super().__init__(settings)
        self.cfg = settings

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    async def fetch(self, client: Any) -> list[Offer]:
        url = f"https://{self.cfg.host}/paapi5/searchitems"
        offers: list[Offer] = []
        seen: set[str] = set()

        for keyword in self.cfg.keywords:
            params = {
                "keywords": keyword,
                "partnerTag": self.cfg.partner_tag,
                "itemCount": "10",
                "searchIndex": "All",
            }
            headers = build_signed_headers(
                method="GET",
                path="/paapi5/searchitems",
                params=params,
                headers={
                    "host": self.cfg.host,
                    "x-amz-target": TARGET,
                    "user-agent": "bot-ofertas-telegram/1.0",
                },
                access_key=self.cfg.access_key,
                secret_key=self.cfg.secret_key,
                region=self.cfg.region,
            )

            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()

            items = _dig(payload, ("data", "SearchResults", "Items")) or []
            for item in items:
                offer = self._parse_item(item)
                if offer is None or offer.url_hash in seen:
                    continue
                seen.add(offer.url_hash)
                offers.append(offer)

        return offers

    def _parse_item(self, item: dict) -> Offer | None:
        asin = item.get("ASIN")
        title = _dig(item, ("ItemInfo", "Title", "DisplayValue"))
        detail_url = item.get("DetailURL")
        if not asin or not title or not detail_url:
            return None

        current = _dig(
            item,
            ("Offers", "Listings", 0, "Price", "Amount"),
            ("Offers", "SummaryPrice", "LowestPrice", "Amount"),
            ("ItemPrice", "Offers", "ListingPrice", "Amount"),
            ("ItemPrice", "LowestPrice", "Amount"),
        )
        original = _dig(
            item,
            ("ItemPrice", "Offers", "ListingPrice", "Amount"),
            ("ItemPrice", "Offers", "LowestPrice", "Amount"),
            ("Offers", "Listings", 0, "Price", "Amount"),
        )
        if not current:
            return None

        discount = _dig(item, ("Offers", "Listings", 0, "Price", "SavingsPercentage"))
        if not discount and original and float(original) > float(current):
            discount = (1 - float(current) / float(original)) * 100

        if self.cfg.partner_tag and "tag=" not in detail_url:
            separator = "&" if "?" in detail_url else "?"
            detail_url = f"{detail_url}{separator}tag={self.cfg.partner_tag}"

        image = _dig(item, ("Images", "Primary", "Medium", "URL"))

        return Offer(
            store=self.name,
            external_id=str(asin),
            title=str(title),
            url=str(detail_url),
            current_price=round(float(current), 2),
            original_price=round(float(original), 2) if original else None,
            discount_pct=round(float(discount), 2) if discount else 0.0,
            image_url=str(image) if image else None,
        )
