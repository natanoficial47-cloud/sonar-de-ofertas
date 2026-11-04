"""Testes do pipeline: filtros, anti-duplicidade, formatacao e publicacao."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from config import Settings
from database import Database
from formatter import brl, build_message
from models import Offer
from pipeline import select_offers
from services.amazon import build_signed_headers
from services.shopee import sign_request
from telegram_client import TelegramPublisher


# --------------------------------------------------------------------- helpers
def make_offer(**overrides) -> Offer:
    data = {
        "store": "Shopee",
        "external_id": "123",
        "title": "Fone Bluetooth",
        "url": "https://shopee.com.br/produto/123",
        "current_price": 100.0,
        "original_price": 200.0,
        "discount_pct": 50.0,
    }
    data.update(overrides)
    return Offer(**data)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_token="token-fake",
        telegram_chat_id="-100123",
        telegram_enabled=True,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        min_discount_pct=35.0,
        min_price=49.9,
        max_price=3000.0,
        max_offers_per_cycle=5,
        resend_cooldown_hours=24,
        price_drop_retrigger_pct=10.0,
        post_demo_offers=True,
        send_delay_seconds=0.0,
        sources=("amazon", "shopee", "mercadolivre"),
    )


@pytest.fixture()
def db(settings: Settings) -> Database:
    database = Database(settings.database_url).connect()
    yield database
    database.close()


class FakeBot:
    def __init__(self, failures: int = 0, error: Exception | None = None):
        self.messages: list[dict] = []
        self.failures = failures
        self.error = error

    async def send_message(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise self.error or RuntimeError("falha temporaria")
        self.messages.append(kwargs)
        return {"message_id": len(self.messages)}


class FakeAdapter:
    name = "Fake"

    def __init__(self, offers: list[Offer]):
        self.offers = offers

    async def collect(self, client, cycle: int = 0) -> list[Offer]:
        return self.offers


# ------------------------------------------------------------------ formatter
def test_formatter_escapa_html_do_titulo():
    offer = make_offer(title="Fone & <b>Pro</b>")
    message = build_message(offer)
    assert "&amp;" in message
    assert "&lt;b&gt;Pro&lt;/b&gt;" in message
    assert "<b>Pro</b>" not in message


def test_formatter_marca_oferta_demo():
    message = build_message(make_offer(is_demo=True))
    assert "DEMO" in message


def test_brl_usa_virgula_decimal():
    assert brl(49.9) == "R$ 49,90"
    assert brl(1234.5) == "R$ 1.234,50"


# --------------------------------------------------------------------- filtros
def test_filtra_desconto_insuficiente(settings, db):
    oferta = make_offer(discount_pct=10.0, original_price=110.0)
    selection = select_offers([oferta], db, settings)
    assert selection.to_send == []
    assert "desconto insuficiente" in selection.skipped[0][1]


def test_filtra_preco_fora_da_faixa(settings, db):
    barata = make_offer(current_price=10.0, url="https://shopee.com.br/a")
    cara = make_offer(current_price=9000.0, url="https://shopee.com.br/b")
    selection = select_offers([barata, cara], db, settings)
    assert selection.to_send == []
    assert selection.skipped_count == 2


def test_deduplica_dentro_do_ciclo(settings, db):
    mesma = make_offer(url="https://shopee.com.br/x", discount_pct=40.0)
    mesma_melhor = make_offer(url="https://shopee.com.br/x", discount_pct=70.0)
    selection = select_offers([mesma, mesma_melhor], db, settings)
    assert len(selection.to_send) == 1
    assert selection.to_send[0].discount_pct == 70.0


def test_respeita_limite_por_ciclo(settings, db):
    ofertas = [make_offer(url=f"https://shopee.com.br/p{i}", discount_pct=60.0 - i) for i in range(8)]
    settings = Settings(**{**settings.__dict__, "max_offers_per_cycle": 3})
    selection = select_offers(ofertas, db, settings)
    assert len(selection.to_send) == 3
    # as melhores primeiro
    assert selection.to_send[0].discount_pct == 60.0


# --------------------------------------------------------- anti-duplicidade
def test_primeira_vazao_e_enviada(settings, db):
    oferta = make_offer()
    send, reason = db.should_send(oferta, cooldown_hours=24, price_drop_retrigger_pct=10)
    assert send is True
    assert reason == "primeira vez"


def test_segunda_vazao_e_bloqueada(settings, db):
    oferta = make_offer()
    db.record_seen(oferta)
    db.mark_sent(oferta)

    send, reason = db.should_send(oferta, cooldown_hours=24, price_drop_retrigger_pct=10)
    assert send is False
    assert "duplicada" in reason


def test_reenvia_depois_do_cooldown(settings, db):
    oferta = make_offer()
    db.record_seen(oferta)
    db.mark_sent(oferta)

    future = int(time.time()) + 25 * 3600
    send, reason = db.should_send(
        oferta, now=future, cooldown_hours=24, price_drop_retrigger_pct=10
    )
    assert send is True
    assert "cooldown" in reason


def test_reenvia_antes_do_cooldown_se_o_preco_cair(settings, db):
    oferta = make_offer(current_price=100.0)
    db.record_seen(oferta)
    db.mark_sent(oferta)

    mais_barato = make_offer(current_price=70.0)
    send, reason = db.should_send(
        mais_barato, cooldown_hours=24, price_drop_retrigger_pct=10
    )
    assert send is True
    assert "preco caiu" in reason


def test_oferta_vista_mas_nao_enviada_e_enviada(settings, db):
    oferta = make_offer()
    db.record_seen(oferta)
    send, reason = db.should_send(oferta)
    assert send is True
    assert "nunca enviada" in reason


# ------------------------------------------------------------- ciclo completo
def test_ciclo_completo_publica_e_nao_duplica(settings, db):
    from main import run_cycle

    ofertas = [
        make_offer(url="https://shopee.com.br/a", external_id="a", discount_pct=50.0),
        make_offer(url="https://shopee.com.br/b", external_id="b", discount_pct=60.0),
    ]
    bot = FakeBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    adapters = [FakeAdapter(ofertas)]

    primeiro = asyncio.run(run_cycle(settings, db, publisher=publisher, adapters=adapters))
    assert primeiro["sent"] == 2
    assert len(bot.messages) == 2

    segundo = asyncio.run(run_cycle(settings, db, publisher=publisher, adapters=adapters))
    assert segundo["sent"] == 0
    assert len(bot.messages) == 2  # nada novo foi enviado


def test_ciclo_registra_run_no_banco(settings, db):
    from main import run_cycle

    bot = FakeBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    asyncio.run(
        run_cycle(
            settings,
            db,
            publisher=publisher,
            adapters=[FakeAdapter([make_offer(discount_pct=80.0)])],
        )
    )
    runs = db.recent_runs()
    assert runs[0]["status"] == "ok"
    assert runs[0]["offers_sent"] == 1


def test_dry_run_nao_envia(settings, db):
    settings = Settings(**{**settings.__dict__, "telegram_enabled": False})
    bot = FakeBot()
    publisher = TelegramPublisher(settings, bot=bot)
    resultado = asyncio.run(publisher.publish([make_offer()]))
    assert resultado == 0
    assert bot.messages == []


def test_publicador_tenta_novamente_apos_falha(settings):
    bot = FakeBot(failures=1)
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    enviados = asyncio.run(publisher.publish([make_offer()]))
    assert enviados == 1
    assert len(bot.messages) == 1


# ------------------------------------------------------------------ assinaturas
def test_shopee_assinatura_conhecida():
    import base64
    import hashlib
    import hmac
    from urllib.parse import quote

    path = "/api/v2/product/get_items"
    params = {"partner_id": "123", "page": 1, "page_size": 50, "timestamp": 1700000000}
    esperado = base64.b64encode(
        hmac.new(
            b"chave-secreta",
            (
                path
                + quote("page", safe="")
                + quote("1", safe="")
                + quote("page_size", safe="")
                + quote("50", safe="")
                + quote("timestamp", safe="")
                + quote("1700000000", safe="")
            ).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert sign_request("chave-secreta", path, params) == esperado


def test_amazon_sigv4_gera_authorization_valido():
    headers = build_signed_headers(
        method="GET",
        path="/paapi5/searchitems",
        params={"keywords": "fone", "partnerTag": "tag-20"},
        headers={
            "host": "webservices.amazon.com",
            "x-amz-target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
        },
        access_key="AKIAEXEMPLO",
        secret_key="segredo",
        region="us-east-1",
        amz_date="20261029T204815Z",
    )
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXEMPLO/20261029/us-east-1/paapi5/aws4_request")
    assert "SignedHeaders=host;x-amz-date;x-amz-target" in auth
    assert "Signature=" in auth
    assert headers["X-Amz-Date"] == "20261029T204815Z"
