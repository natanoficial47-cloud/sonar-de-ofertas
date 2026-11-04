"""Testes dos adaptadores: fallback demo, isolamento de falhas e parsing."""

from __future__ import annotations

import asyncio

from config import AmazonSettings, MercadoLivreSettings, ShopeeSettings
from services import AmazonAdapter, MercadoLivreAdapter, ShopeeAdapter
from services.base import StoreAdapter


class FakeClient:
    def __init__(self, payload: dict | None = None, status: int = 200):
        self.payload = payload or {}
        self.status = status
        self.calls: list[dict] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})

        class Response:
            def __init__(self, payload, status):
                self._payload = payload
                self.status_code = status
                self.text = str(payload)

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        return Response(self.payload, self.status)


def test_sem_credencial_o_adapter_devolve_demo():
    adapter = AmazonAdapter(AmazonSettings())
    ofertas = asyncio.run(adapter.collect(FakeClient()))
    assert ofertas
    assert all(o.is_demo for o in ofertas)
    assert all(o.store == "Amazon" for o in ofertas)


def test_mercado_livre_sem_token_cai_para_demo():
    adapter = MercadoLivreAdapter(MercadoLivreSettings())
    assert adapter.configured is False
    ofertas = asyncio.run(adapter.collect(FakeClient()))
    assert ofertas and all(o.is_demo for o in ofertas)


def test_loja_configurada_com_api_quebrada_nao_publica_mock():
    """Falha de API nao pode virar oferta ficticia no grupo."""
    adapter = ShopeeAdapter(
        ShopeeSettings(partner_id="1", partner_key="k", shop_id="9", keywords=("fone",))
    )
    client = FakeClient({"items": []}, status=500)
    ofertas = asyncio.run(adapter.collect(client))
    assert ofertas == []


def test_amazon_parse_do_payload_da_api():
    payload = {
        "data": {
            "SearchResults": {
                "Items": [
                    {
                        "ASIN": "B0TESTE123",
                        "DetailURL": "https://www.amazon.com.br/dp/B0TESTE123",
                        "ItemInfo": {"Title": {"DisplayValue": "Fone Bluetooth Pro"}},
                        "ItemPrice": {"Offers": {"ListingPrice": {"Amount": 200.0}}},
                        "Offers": {
                            "Listings": [
                                {"Price": {"Amount": 100.0}, "SavingBasis": "50%"}
                            ]
                        },
                    }
                ]
            }
        }
    }
    adapter = AmazonAdapter(
        AmazonSettings(
            access_key="a",
            secret_key="b",
            partner_id="c",
            partner_tag="minha-tag",
            keywords=("fone",),
        )
    )
    ofertas = asyncio.run(adapter.fetch(FakeClient(payload)))
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.external_id == "B0TESTE123"
    assert oferta.current_price == 100.0
    assert oferta.original_price == 200.0
    assert oferta.effective_discount == 50.0
    assert "tag=minha-tag" in oferta.url


def test_mercado_livre_parse_do_payload_da_api():
    payload = {
        "results": [
            {
                "id": "MLB123",
                "title": "Cadeira Gamer",
                "price": 900.0,
                "original_price": 1200.0,
                "discount_percentage": 25,
                "permalink": "https://mercadolivre.com.br/item/MLB123",
                "thumbnail": "http://img",
            }
        ]
    }
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(access_token="token", affiliate_id="afiliate")
    )
    ofertas = asyncio.run(adapter.fetch(FakeClient(payload)))
    assert len(ofertas) == 1
    assert ofertas[0].current_price == 900.0
    assert "tag=afiliate" in ofertas[0].url


def test_shopee_parse_de_precos_em_centavos():
    payload = {
        "items": [
            {
                "itemid": 999,
                "name": "Fone Bluetooth TWS",
                "price_min": 12990,
                "normal_price": 25990,
                "discount_percentage": "50",
                "shopid": 12345,
            }
        ]
    }
    adapter = ShopeeAdapter(
        ShopeeSettings(partner_id="1", partner_key="k", keywords=("fone",))
    )
    ofertas = asyncio.run(adapter.fetch(FakeClient(payload)))
    assert len(ofertas) == 1
    assert ofertas[0].current_price == 129.90
    assert ofertas[0].original_price == 259.90
    assert "12345" in ofertas[0].url
    assert ofertas[0].url.endswith("-999")


def test_collect_e_ponto_de_entrada_unificado():
    assert issubclass(AmazonAdapter, StoreAdapter)
    assert issubclass(ShopeeAdapter, StoreAdapter)
    assert issubclass(MercadoLivreAdapter, StoreAdapter)
