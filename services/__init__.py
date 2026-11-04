"""Adaptadores de loja (Amazon, Shopee, Mercado Livre)."""

from services.amazon import AmazonAdapter
from services.base import StoreAdapter
from services.mercadolivre import MercadoLivreAdapter
from services.shopee import ShopeeAdapter

__all__ = [
    "AmazonAdapter",
    "MercadoLivreAdapter",
    "ShopeeAdapter",
    "StoreAdapter",
]
