"""Testes dos adaptadores: fallback demo, isolamento de falhas e parsing."""

from __future__ import annotations

import asyncio

from config import AmazonSettings, MercadoLivreSettings, ShopeeSettings
from formatter import build_message
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


def test_mercado_livre_troca_client_credentials_por_token():
    """O adapter obtem o access token sozinho a partir de id+secret."""
    from services.mercadolivre import fetch_access_token

    class OAuthClient(FakeClient):
        async def post(self, url, data=None, **kwargs):
            self.posts = getattr(self, "posts", [])
            self.posts.append({"url": url, "data": data})
            return type(
                "R",
                (),
                {
                    "json": lambda self: {
                        "access_token": "APP_USR-1-2-abc",
                        "expires_in": 21600,
                    }
                },
            )()

    client = OAuthClient()
    token = asyncio.run(fetch_access_token(client, "123", "secret"))
    assert token == "APP_USR-1-2-abc"
    assert client.posts[0]["data"]["grant_type"] == "client_credentials"
    assert client.posts[0]["data"]["client_id"] == "123"


def test_mercado_livre_configurada_com_client_id_e_secret():
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(client_id="123", client_secret="secret")
    )
    assert adapter.configured is True


def test_loja_configurada_com_api_quebrada_nao_publica_mock():
    """Falha de API nao pode virar oferta ficticia no grupo."""
    adapter = ShopeeAdapter(
        ShopeeSettings(
            partner_id="1",
            partner_key="k",
            shop_id="9",
            access_token="token",
            keywords=("fone",),
        )
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


class RoutingClient(FakeClient):
    """FakeClient que devolve payloads diferentes conforme a URL chamada."""

    def __init__(self, routes: dict[str, tuple[dict, int]]):
        super().__init__()
        self.routes = routes

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        payload, status = self.routes.get(url, ({}, 404))

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

        return Response(payload, status)


def test_mercado_livre_parse_do_payload_da_api():
    """Fluxo /products/search -> /products/{id}/items (recursos liberados)."""
    search_payload = {
        "results": [
            {
                "catalog_product_id": "MLB123",
                "name": "Cadeira Gamer Ergonômica",
                "pictures": [{"url": "http://img/cadeira.jpg"}],
            }
        ]
    }
    items_payload = {
        "results": [
            {
                "item_id": "MLB999",
                "price": 900.0,
                "original_price": 1200.0,
                "shipping": {"free_shipping": True},
            },
            # Outro vendedor perto do preco de tabela: confirma o desconto.
            {"item_id": "MLB998", "price": 1150.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token",
            affiliate_id="afiliate",
            keywords=("cadeira gamer",),
        )
    )
    ofertas = asyncio.run(adapter.fetch(client))
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.external_id == "MLB999"
    assert oferta.current_price == 900.0
    assert oferta.original_price == 1200.0
    assert oferta.effective_discount == 25.0
    assert "/p/MLB123" in oferta.url
    assert "item_id=MLB999" in oferta.url
    assert "tag=afiliate" in oferta.url
    assert oferta.free_shipping is True
    assert oferta.shipping_cost is None


def test_mercado_livre_marca_o_menor_preco_do_produto():
    """Sem preco de tabela, o anuncio mais barato entre os vendedores conta."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Fone Bluetooth"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 100.0},
            {"item_id": "MLB2", "price": 30.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token",
            affiliate_id="afiliate",
            keywords=("fone bluetooth",),
        )
    )
    ofertas = {o.external_id: o for o in asyncio.run(adapter.fetch(client))}
    # So o mais barato vira oferta: e o que a pagina do ML mostra.
    assert set(ofertas) == {"MLB2"}
    mais_barato = ofertas["MLB2"]
    # Mediana de [30, 100] = 65
    assert mais_barato.comparison_price == 65.0
    assert mais_barato.comparison_count == 2
    assert mais_barato.effective_comparison_pct == 53.85


def test_mercado_livre_escolhe_o_anuncio_com_frete_gratis_e_nao_o_mais_barato():
    """O preco anunciado tem de ser o que a pagina mostra: frete incluso.

    Caso do mouse AOAS V02: um anuncio de R$ 29,99 com R$ 46,84 de frete
    (R$ 76,83 no total) contra outro de R$ 49,99 com frete gratis. O
    Mercado Livre destaca o segundo, entao e esse que vai no link.
    """
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Mouse Gamer Aoas V02"}]
    }
    items_payload = {
        "results": [
            {
                "item_id": "MLB1",
                "price": 29.99,
                "shipping": {"free_shipping": False, "cost": 46.84},
            },
            {
                "item_id": "MLB2",
                "price": 49.99,
                "shipping": {"free_shipping": True, "cost": 0},
            },
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("mouse gamer",)
        )
    )
    ofertas = asyncio.run(adapter.fetch(client))
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.external_id == "MLB2"
    assert oferta.current_price == 49.99
    assert oferta.free_shipping is True
    mensagem = build_message(oferta)
    assert "Frete: grátis" in mensagem
    assert "29,99" not in mensagem


def test_mercado_livre_sem_frete_gratis_compara_o_preco_final():
    """Sem frete gratis, vence o anuncio com o menor preco final (produto+frete)."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Fone Bluetooth"}]
    }
    items_payload = {
        "results": [
            {
                "item_id": "MLB1",
                "price": 40.0,
                "shipping": {"free_shipping": False, "cost": 46.84},
            },
            {
                "item_id": "MLB2",
                "price": 55.0,
                "shipping": {"free_shipping": False, "cost": 0},
            },
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("fone",)
        )
    )
    oferta = asyncio.run(adapter.fetch(client))[0]
    # MLB1 sai por R$ 86,84 no total; MLB2 sai por R$ 55,00.
    assert oferta.external_id == "MLB2"
    assert oferta.current_price == 55.0
    # Frete 0 e frete gratis, mesmo sem a flag do ML.
    assert oferta.free_shipping is True
    assert oferta.shipping_cost is None
    assert "Frete: R$ 0,00" not in build_message(oferta)
    assert "Frete: grátis" in build_message(oferta)


def test_mercado_livre_desconto_de_tabela_confirmado_pelo_mercado():
    """Quando outro vendedor cobra perto do preco de tabela, o OFF vale."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Cadeira Gamer"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 799.9, "original_price": 1299.9},
            {"item_id": "MLB2", "price": 1250.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("cadeira",)
        )
    )
    ofertas = asyncio.run(adapter.fetch(client))
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.effective_discount == 38.46
    assert "38% OFF" in build_message(oferta)

def test_mercado_livre_preco_de_tabela_do_anuncio_vira_off_sozinho():
    """O preco de tabela e do anuncio linkado: vale mesmo com um so vendedor.

    Foi o caso da cadeira gamer (R$ 799,90 de R$ 1.299,90): o valor riscado
    aparece na pagina do proprio item, entao o desconto e verdadeiro.
    """
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Cadeira Gamer"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 799.9, "original_price": 1299.9},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("cadeira",)
        )
    )
    ofertas = asyncio.run(adapter.fetch(client))
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.current_price == 799.9
    assert oferta.original_price == 1299.9
    assert oferta.effective_discount == 38.46
    # Sem outro vendedor para corroborar, a mensagem nao inventa essa certeza.
    assert oferta.table_corroborated is False
    mensagem = build_message(oferta)
    assert "38% OFF" in mensagem
    assert "mesmo preço em outros vendedores" not in mensagem


def test_mercado_livre_preco_de_tabela_corroborado_por_outro_vendedor():
    """Outro vendedor no mesmo preco de tabela corrobora o desconto."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Cadeira Gamer"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 799.9, "original_price": 1299.9},
            {"item_id": "MLB2", "price": 1250.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("cadeira",)
        )
    )
    oferta = asyncio.run(adapter.fetch(client))[0]
    assert oferta.table_corroborated is True
    assert "mesmo preço em outros vendedores do produto" in build_message(oferta)


def test_mensagem_mostra_a_economia_frente_ao_preco_tipico():
    """Sem preco de tabela, a mensagem mostra de onde vem o desconto."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Fone Bluetooth"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 100.0},
            {"item_id": "MLB2", "price": 30.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("fone",)
        )
    )
    oferta = asyncio.run(adapter.fetch(client))[0]
    mensagem = build_message(oferta)
    assert "Menor preço entre 2 ofertas" in mensagem
    assert "Economia de 54%" in mensagem


def test_mercado_livre_desconto_de_tabela_tem_precedencia():
    """Com original_price no anuncio mais barato, o desconto real manda."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Fone Bluetooth"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 30.0, "original_price": 60.0},
            {"item_id": "MLB2", "price": 50.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("fone",)
        )
    )
    ofertas = {o.external_id: o for o in asyncio.run(adapter.fetch(client))}
    assert set(ofertas) == {"MLB1"}
    com_tabela = ofertas["MLB1"]
    assert com_tabela.effective_discount == 50.0
    assert com_tabela.effective_comparison_pct == 0.0
    assert "50% OFF" in build_message(com_tabela)


def test_mercado_livre_token_vencido_e_renovado():
    """Token em cache vencido (401) precisa ser trocado, nao desistir."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Fone Bluetooth"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 30.0, "original_price": 60.0},
            {"item_id": "MLB2", "price": 50.0},
        ]
    }

    class TokenVencido(RoutingClient):
        def __init__(self, routes):
            super().__init__(routes)
            self.trocas = 0
            self.tokens_usados: list[str] = []

        async def post(self, url, data=None, **kwargs):
            self.trocas += 1
            return type(
                "R",
                (),
                {"json": lambda self: {"access_token": "NOVO", "expires_in": 21600}},
            )()

        async def get(self, url, params=None, headers=None):
            self.tokens_usados.append((headers or {}).get("Authorization", ""))
            if self.tokens_usados[-1] == "Bearer VENCIDO":
                response = await super().get(url, params=params, headers=headers)
                response.status_code = 401
                return response
            return await super().get(url, params=params, headers=headers)

    client = TokenVencido(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="VENCIDO",
            client_id="123",
            client_secret="secret",
            keywords=("fone",),
        )
    )
    ofertas = asyncio.run(adapter.fetch(client))
    assert client.trocas == 1
    assert adapter.refreshed_access_token == "NOVO"
    assert ofertas and ofertas[0].external_id == "MLB1"


def test_mercado_livre_produto_sem_ofertas_nao_inventa_item():
    """'No winners found' (404) precisa virar lista vazia, nao oferta."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Cadeira Gamer"}]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("cadeira gamer",)
        )
    )
    ofertas = asyncio.run(adapter.fetch(client))
    assert ofertas == []


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


def test_offers_sources_usa_a_chave_de_cada_loja():
    """'mercadolivre' precisa casar com a loja (o nome tem espaco)."""
    from dataclasses import replace

    from config import Settings
    from main import build_adapters

    settings = replace(Settings.from_env(), sources=("mercadolivre",))
    adapters = build_adapters(settings)
    assert len(adapters) == 1
    assert adapters[0].source_key == "mercadolivre"



def test_comparacao_absurda_nao_vira_desconto():
    """Anuncio muito abaixo do preco tipico e outro produto, nao desconto."""
    search_payload = {
        "results": [{"catalog_product_id": "MLB123", "name": "Livro"}]
    }
    items_payload = {
        "results": [
            {"item_id": "MLB1", "price": 30.0},
            {"item_id": "MLB2", "price": 450.0},
        ]
    }
    client = RoutingClient(
        {
            "https://api.mercadolibre.com/products/search": (search_payload, 200),
            "https://api.mercadolibre.com/products/MLB123/items": (items_payload, 200),
        }
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(
            access_token="token", affiliate_id="afiliate", keywords=("livro",)
        )
    )
    oferta = asyncio.run(adapter.fetch(client))[0]
    assert oferta.current_price == 30.0
    # Mediana = 240; 30 e menos de 35% disso -> nao entra como desconto.
    assert oferta.comparison_price is None
    assert oferta.effective_comparison_pct == 0.0
    assert "Menor preço entre" not in build_message(oferta)


# ------------------------------------------------------- preflight (ping)
def test_ping_do_mercado_livre_confere_o_token():
    """O preflight do deploy precisa dizer se o token vale."""
    search_payload = {"results": [{"catalog_product_id": "MLB1", "name": "Fone"}]}
    client = RoutingClient(
        {"https://api.mercadolibre.com/products/search": (search_payload, 200)}
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(access_token="token", keywords=("fone",))
    )
    detalhe = asyncio.run(adapter.ping(client))
    assert "token valido" in detalhe


def test_ping_do_mercado_livre_falha_quando_o_token_e_recusado():
    client = RoutingClient(
        {"https://api.mercadolibre.com/products/search": ({}, 401)}
    )
    adapter = MercadoLivreAdapter(
        MercadoLivreSettings(access_token="vencido", keywords=("fone",))
    )
    try:
        asyncio.run(adapter.ping(client))
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("o ping deveria ter falhado com token recusado")


def test_amazon_falha_em_uma_keyword_nao_derruba_as_outras():
    """Uma keyword quebrada nao pode zerar a coleta da loja inteira."""

    class ClientAmazon(FakeClient):
        async def get(self, url, params=None, headers=None):
            self.calls.append({"url": url, "params": params, "headers": headers})
            if "quebrada" in (params or {}).get("keywords", ""):
                raise RuntimeError("503 do provedor")

            class Response:
                status_code = 200

                def __init__(self):
                    self._payload = {
                        "data": {
                            "SearchResults": {
                                "Items": [
                                    {
                                        "ASIN": "B0OK",
                                        "DetailURL": "https://amazon.com.br/dp/B0OK",
                                        "ItemInfo": {
                                            "Title": {"DisplayValue": "Fone Bom"}
                                        },
                                        "ItemPrice": {
                                            "Offers": {
                                                "ListingPrice": {"Amount": 200.0}
                                            }
                                        },
                                        "Offers": {
                                            "Listings": [{"Price": {"Amount": 100.0}}]
                                        },
                                    }
                                ]
                            }
                        }
                    }

                def json(self):
                    return self._payload

                def raise_for_status(self):
                    return None

            return Response()

    from config import AmazonSettings

    adapter = AmazonAdapter(
        AmazonSettings(
            access_key="AKIA",
            secret_key="segredo",
            partner_tag="natannunes-20",
            keywords=("quebrada", "boa"),
        )
    )
    ofertas = asyncio.run(adapter.fetch(ClientAmazon()))
    assert [o.external_id for o in ofertas] == ["B0OK"]
