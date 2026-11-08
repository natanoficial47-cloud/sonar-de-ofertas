"""Catalogo de ofertas de demonstracao.

Usado quando a loja nao tem credencial configurada. Os precos variam com o
numero do ciclo para simular o dia a dia: assim a regra de reenvio por queda
de preco continua verificavel mesmo sem API real.
"""

from __future__ import annotations

from models import Offer

_CATALOG: dict[str, list[dict]] = {
    "Amazon": [
        {
            "external_id": "DEMO-AMZ-001",
            "title": "Echo Dot 5ª geração (Alexa)",
            "url": "https://www.amazon.com.br/dp/demo-echo-dot",
            "base_price": 349.9,
        },
        {
            "external_id": "DEMO-AMZ-002",
            "title": "Fire TV Stick 4K - 2ª geração",
            "url": "https://www.amazon.com.br/dp/demo-fire-tv",
            "base_price": 279.9,
        },
        {
            "external_id": "DEMO-AMZ-003",
            "title": "Smartwatch、智能運動 - Compatível com Android e iOS",
            "url": "https://www.amazon.com.br/dp/demo-smartwatch",
            "base_price": 249.9,
        },
        {
            "external_id": "DEMO-AMZ-004",
            "title": "Fone Bluetooth com Cancelamento de Ruído",
            "url": "https://www.amazon.com.br/dp/demo-fone-bt",
            "base_price": 199.9,
        },
    ],
    "Shopee": [
        {
            "external_id": "DEMO-SHP-001",
            "title": "Fone Bluetooth TWS Pro - 30h de bateria",
            "url": "https://shopee.com.br/product/demo-fone-tws",
            "base_price": 129.9,
        },
        {
            "external_id": "DEMO-SHP-002",
            "title": "Tênis Esportivo Unissex Running Leve",
            "url": "https://shopee.com.br/product/demo-tenis",
            "base_price": 219.9,
        },
        {
            "external_id": "DEMO-SHP-003",
            "title": "Watch Digital Esportivo à Prova d'Água",
            "url": "https://shopee.com.br/product/demo-watch-digital",
            "base_price": 99.9,
        },
        {
            "external_id": "DEMO-SHP-004",
            "title": "Mochila Antifurto com Porta USB",
            "url": "https://shopee.com.br/product/demo-mochila",
            "base_price": 179.9,
        },
    ],
    "Mercado Livre": [
        {
            "external_id": "DEMO-ML-001",
            "title": "Cadeira Gamer ergonômica reclinável",
            "url": "https://mercadolivre.com.br/item/demo-cadeira",
            "base_price": 899.9,
        },
        {
            "external_id": "DEMO-ML-002",
            "title": "Caixa de Som Portátil Bluetooth à prova d'água",
            "url": "https://mercadolivre.com.br/item/demo-caixa-som",
            "base_price": 259.9,
        },
        {
            "external_id": "DEMO-ML-003",
            "title": "Fone Bluetooth Sport com Microfone",
            "url": "https://mercadolivre.com.br/item/demo-fone-sport",
            "base_price": 149.9,
        },
        {
            "external_id": "DEMO-ML-004",
            "title": "Notebook 15,6'' - 8GB RAM, SSD 256GB",
            "url": "https://mercadolivre.com.br/item/demo-notebook",
            "base_price": 2499.9,
        },
    ],
}


def mock_offers_for(store: str, cycle: int = 0) -> list[Offer]:
    """Gera ofertas de demonstracao para a loja informada."""
    items = _CATALOG.get(store, [])
    offers: list[Offer] = []
    for index, item in enumerate(items):
        # Desconto gira entre 45% e 75% e muda a cada ciclo.
        discount = 45 + ((cycle * 7 + index * 13) % 31)
        base = float(item["base_price"])
        current = round(base * (1 - discount / 100), 2)
        offers.append(
            Offer(
                store=store,
                external_id=str(item["external_id"]),
                product_id=str(item["external_id"]),
                title=str(item["title"]),
                url=str(item["url"]),
                current_price=current,
                original_price=base,
                discount_pct=float(discount),
                is_demo=True,
            )
        )
    return offers
