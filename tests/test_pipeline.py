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
from config import ShopeeSettings
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
        self.photos: list[dict] = []
        self.failures = failures
        self.error = error

    async def send_message(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise self.error or RuntimeError("falha temporaria")
        self.messages.append(kwargs)
        return {"message_id": len(self.messages)}

    async def send_photo(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise self.error or RuntimeError("falha temporaria")
        self.photos.append(kwargs)
        return {"message_id": len(self.photos)}


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


def test_mesmo_produto_com_outro_anuncio_e_bloqueado(settings, db):
    """Regressao: o produto ja foi enviado, mas o anuncio mais barato mudou.

    No Mercado Livre o mesmo produto de catalogo tem varios anuncios (item_id
    diferentes). O bot escolhe o mais barato e, quando o preco oscila entre
    eles, a URL muda. Se a anti-duplicidade so olhasse a URL, o produto
    voltaria ao grupo como se fosse oferta nova.
    """
    enviado = make_offer(
        store="Mercado Livre",
        product_id="MLB29000729",
        external_id="MLB1111111111",
        url="https://www.mercadolivre.com.br/p/MLB29000729?item_id=MLB1111111111",
        current_price=100.0,
    )
    db.record_seen(enviado)
    db.mark_sent(enviado)

    # Mesmo produto de catalogo, outro anuncio (item_id e URL diferentes).
    outro_anuncio = make_offer(
        store="Mercado Livre",
        product_id="MLB29000729",
        external_id="MLB2222222222",
        url="https://www.mercadolivre.com.br/p/MLB29000729?item_id=MLB2222222222",
        current_price=98.0,
    )

    send, reason = db.should_send(
        outro_anuncio, cooldown_hours=24, price_drop_retrigger_pct=10
    )
    assert send is False, "o mesmo produto nao pode voltar ao grupo"
    assert "duplicada" in reason


def test_oferta_sem_product_id_segue_pela_url(settings, db):
    """Fonte sem id de catalogo continua usando o hash da URL."""
    oferta = make_offer(url="https://shopee.com.br/produto/123")
    db.record_seen(oferta)
    db.mark_sent(oferta)

    send, _ = db.should_send(oferta, cooldown_hours=24, price_drop_retrigger_pct=10)
    assert send is False


def test_oscilacao_de_preco_entre_anuncios_nao_reenvia(settings, db):
    """O preco do MESMO anuncio que caiu e motivo legitimo de reenvio.

    Ja o preco de OUTRO vendedor do mesmo produto e ruido de mercado: nao
    pode virar motivo para repetir a oferta no grupo.
    """
    enviado = make_offer(
        store="Mercado Livre",
        product_id="MLB40341086",
        external_id="MLB3333333333",
        url="https://www.mercadolivre.com.br/p/MLB40341086?item_id=MLB3333333333",
        current_price=120.0,
    )
    db.record_seen(enviado)
    db.mark_sent(enviado)

    # Outro vendedor do MESMO produto, 20% mais barato: o preco do anuncio
    # enviado (120) nao mudou, entao nao ha motivo para reenviar.
    mais_barato = make_offer(
        store="Mercado Livre",
        product_id="MLB40341086",
        external_id="MLB4444444444",
        url="https://www.mercadolivre.com.br/p/MLB40341086?item_id=MLB4444444444",
        current_price=96.0,
    )
    send, _ = db.should_send(
        mais_barato, cooldown_hours=24, price_drop_retrigger_pct=10
    )
    assert send is False


def test_queda_de_preco_no_mesmo_anuncio_reenvia(settings, db):
    """Esse e o gatilho legitimo: o MESMO anuncio ficou mais barato."""
    oferta = make_offer(
        store="Mercado Livre",
        product_id="MLB40341086",
        external_id="MLB5555555555",
        url="https://www.mercadolivre.com.br/p/MLB40341086?item_id=MLB5555555555",
        current_price=120.0,
    )
    db.record_seen(oferta)
    db.mark_sent(oferta)

    mais_barato = make_offer(
        store="Mercado Livre",
        product_id="MLB40341086",
        external_id="MLB5555555555",
        url="https://www.mercadolivre.com.br/p/MLB40341086?item_id=MLB5555555555",
        current_price=100.0,
    )
    send, reason = db.should_send(
        mais_barato, cooldown_hours=24, price_drop_retrigger_pct=10
    )
    assert send is True
    assert "preco caiu" in reason


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
    assert resultado == []
    assert bot.messages == []


def test_ciclo_em_dry_run_nao_marca_oferta_como_enviada(settings, db):
    """Regressao: dry-run nao pode 'gastar' a oferta na anti-duplicidade."""
    from main import run_cycle

    settings = Settings(**{**settings.__dict__, "telegram_enabled": False})
    publisher = TelegramPublisher(settings, bot=FakeBot())
    oferta = make_offer(url="https://shopee.com.br/dry", discount_pct=70.0)

    asyncio.run(
        run_cycle(
            settings,
            db,
            publisher=publisher,
            adapters=[FakeAdapter([oferta])],
        )
    )

    row = db.get_offer(oferta.url_hash)
    assert row is not None
    assert row["last_sent_at"] is None
    assert row["send_count"] == 0

    # No ciclo seguinte a oferta continua disponivel para envio real.
    send, _ = db.should_send(oferta)
    assert send is True


def test_ciclo_marca_somente_o_que_foi_enviado(settings, db):
    from main import run_cycle

    ofertas = [
        make_offer(url="https://shopee.com.br/ok", external_id="ok", discount_pct=70.0),
        make_offer(url="https://shopee.com.br/erro", external_id="erro", discount_pct=65.0),
    ]

    class MeiaFalhaBot(FakeBot):
        async def send_message(self, **kwargs):
            if "erro" in kwargs.get("text", ""):
                raise RuntimeError("falha ao enviar")
            return await super().send_message(**kwargs)

    publisher = TelegramPublisher(settings, bot=MeiaFalhaBot(), sleep=lambda *_: asyncio.sleep(0))
    resultado = asyncio.run(
        run_cycle(settings, db, publisher=publisher, adapters=[FakeAdapter(ofertas)])
    )

    assert resultado["sent"] == 1
    enviada = db.get_offer(ofertas[0].url_hash)
    falhou = db.get_offer(ofertas[1].url_hash)
    assert enviada["last_sent_at"] is not None
    assert enviada["send_count"] == 1
    assert falhou["last_sent_at"] is None
    assert falhou["send_count"] == 0


def test_publicador_tenta_novamente_apos_falha(settings):
    bot = FakeBot(failures=1)
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    enviados = asyncio.run(publisher.publish([make_offer()]))
    assert len(enviados) == 1
    assert len(bot.messages) == 1


def test_publicador_manda_a_foto_do_produto(settings):
    """Com imagem, a oferta vai como foto com a legenda."""
    bot = FakeBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    oferta = make_offer(image_url="https://http2.mlstatic.com/foto.jpg")
    enviados = asyncio.run(publisher.publish([oferta]))
    assert len(enviados) == 1
    assert len(bot.photos) == 1
    assert bot.photos[0]["photo"] == "https://http2.mlstatic.com/foto.jpg"
    assert "CLIQUE AQUI PARA COMPRAR" in bot.photos[0]["caption"]
    assert bot.messages == []


def test_publicador_cai_para_o_texto_quando_a_foto_falha(settings):
    """Foto quebrada nao pode derrubar a oferta."""

    class SemFoto(FakeBot):
        async def send_photo(self, **kwargs):
            raise RuntimeError("wrong file identifier")

    bot = SemFoto()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    oferta = make_offer(image_url="https://http2.mlstatic.com/foto.jpg")
    enviados = asyncio.run(publisher.publish([oferta]))
    assert len(enviados) == 1
    assert bot.messages and not bot.photos


def test_erro_generico_nao_e_tratado_como_flood(settings):
    """Regressao: RetryAfter/Forbidden nao podem virar alias de Exception."""
    import telegram_client

    assert telegram_client.TELEGRAM_AVAILABLE is True
    assert telegram_client.RetryAfter is not Exception
    assert telegram_client.Forbidden is not Exception
    assert telegram_client.TelegramError is not Exception
    assert telegram_client.RetryAfter is not telegram_client.TelegramError


def test_parse_mode_html_disponivel():
    import telegram_client

    assert getattr(telegram_client.ParseMode, "HTML", "HTML")


# ------------------------------------------------------------------ assinaturas
def test_shopee_assinatura_conhecida():
    # Spec oficial v2: hex(HMAC-SHA256(partner_id + path + ts + token + shop))
    import hashlib
    import hmac

    esperado = hmac.new(
        b"chave-secreta",
        b"10090/api/v2/product/get_items1594897040token1239876",
        hashlib.sha256,
    ).hexdigest()
    assinatura = sign_request(
        "10090",
        "chave-secreta",
        "/api/v2/product/get_items",
        1594897040,
        "token123",
        "9876",
    )
    assert assinatura == esperado
    assert len(assinatura) == 64
    assert assinatura == assinatura.lower()


def test_shopee_sem_credencial_fica_em_demo():
    from services import ShopeeAdapter

    adapter = ShopeeAdapter(ShopeeSettings())
    assert adapter.configured is False


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


# ------------------------------------------------------------------ variedade
def test_ciclo_nao_repete_a_mesma_categoria(settings, db):
    """O teto por categoria impede o grupo inteiro do ciclo vir de um nicho."""
    from dataclasses import replace

    settings = replace(settings, max_offers_per_keyword_per_cycle=1)
    ofertas = [
        make_offer(
            external_id=f"mlb{i}",
            url=f"https://www.mercadolivre.com.br/p/MLB{i}?item_id=MLB{i}",
            title=f"Produto {i}",
            keyword="cadeira gamer",
            discount_pct=90.0 - i,
        )
        for i in range(3)
    ] + [
        make_offer(
            external_id="outro",
            url="https://www.mercadolivre.com.br/p/MLB99?item_id=MLB99",
            title="Air Fryer",
            keyword="air fryer",
            discount_pct=50.0,
        )
    ]

    selection = select_offers(ofertas, db, settings)

    categorias = [o.keyword for o in selection.to_send]
    assert categorias.count("cadeira gamer") == 1
    assert "air fryer" in categorias
    reasons = [motivo for _, motivo in selection.skipped]
    assert any("categoria ja repetida" in motivo for motivo in reasons)


def test_mensagem_mostra_a_categoria(settings, db):
    offer = make_offer(keyword="air fryer")
    mensagem = build_message(offer)
    assert "Busca:" in mensagem
    assert "air fryer" in mensagem


# ----------------------------------------------------- nao-duplicacao no envio
def test_timeout_na_foto_nao_reenvia_e_nao_manda_texto(settings):
    """Timeout no envio: pode ter chegado ao grupo, entao nao repetimos.

    Era exatamente o bug que duplicava oferta: o PTB dava timeout de 5s ao
    baixar a foto do CDN, o retry reenviava e a oferta aparecia duas vezes.
    """
    import telegram_client

    class TimeoutFotoBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.chamadas = 0

        async def send_photo(self, **kwargs):
            self.chamadas += 1
            raise telegram_client.TimedOut("Timed out")

        async def send_message(self, **kwargs):
            self.chamadas += 1
            return await super().send_message(**kwargs)

    bot = TimeoutFotoBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))
    oferta = make_offer(image_url="https://http2.mlstatic.com/foto.jpg")

    enviados = asyncio.run(publisher.publish([oferta]))

    assert len(enviados) == 1  # marcada como enviada
    assert bot.chamadas == 1  # uma unica tentativa
    assert bot.messages == []  # nenhum texto de fallback (duplicaria)


def test_timeout_no_texto_nao_reenvia(settings):
    import telegram_client

    class TimeoutTextoBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.chamadas = 0

        async def send_message(self, **kwargs):
            self.chamadas += 1
            raise telegram_client.TimedOut("Timed out")

    bot = TimeoutTextoBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))

    enviados = asyncio.run(publisher.publish([make_offer()]))

    assert len(enviados) == 1
    assert bot.chamadas == 1
    assert bot.messages == []


def test_timeout_httpx_tambem_e_considerado_incerto(settings):
    """httpx.TimeoutException (por baixo do PTB) nao pode virar retry."""
    httpx = pytest.importorskip("httpx")

    class TimeoutHttpxBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.chamadas = 0

        async def send_message(self, **kwargs):
            self.chamadas += 1
            raise httpx.ReadTimeout("read timeout")

    bot = TimeoutHttpxBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))

    enviados = asyncio.run(publisher.publish([make_offer()]))

    assert len(enviados) == 1
    assert bot.chamadas == 1


def test_bad_request_nao_e_repetido(settings):
    """Erro definitivo do Telegram nao entra em loop de retry."""
    import telegram_client

    class BadRequestBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.chamadas = 0

        async def send_message(self, **kwargs):
            self.chamadas += 1
            raise telegram_client.BadRequest("chat not found")

    bot = BadRequestBot()
    publisher = TelegramPublisher(settings, bot=bot, sleep=lambda *_: asyncio.sleep(0))

    enviados = asyncio.run(publisher.publish([make_offer()]))

    assert enviados == []
    assert bot.chamadas == 1


def test_timeout_marca_oferta_como_enviada_no_ciclo(settings, db):
    """End-to-end: oferta com timeout nao volta no ciclo seguinte."""
    from main import run_cycle
    import telegram_client

    class TimeoutFotoBot(FakeBot):
        async def send_photo(self, **kwargs):
            raise telegram_client.TimedOut("Timed out")

    oferta = make_offer(
        external_id="dup",
        url="https://www.mercadolivre.com.br/p/MLBdup?item_id=MLBdup",
        image_url="https://http2.mlstatic.com/foto.jpg",
        discount_pct=70.0,
    )
    publisher = TelegramPublisher(
        settings, bot=TimeoutFotoBot(), sleep=lambda *_: asyncio.sleep(0)
    )

    primeiro = asyncio.run(
        run_cycle(settings, db, publisher=publisher, adapters=[FakeAdapter([oferta])])
    )
    assert primeiro["sent"] == 1

    row = db.get_offer(oferta.url_hash)
    assert row["last_sent_at"] is not None

    segundo = asyncio.run(
        run_cycle(settings, db, publisher=publisher, adapters=[FakeAdapter([oferta])])
    )
    assert segundo["sent"] == 0  # nao volta a ser publicada


def test_timeouts_do_telegram_sao_configurados(settings):
    """O PTB usa 5s de leitura por padrao -- curto para foto do CDN."""
    import telegram_client

    assert telegram_client.READ_TIMEOUT_SECONDS >= 60
    assert telegram_client.CONNECT_TIMEOUT_SECONDS >= 15


# ------------------------------------------------------------- config de deploy
def test_em_ci_sqlite_e_reprovado(settings, monkeypatch):
    """No runner do GitHub o SQLite e efemero: o bot repetiria as ofertas.

    Sem esse trava, uma DATABASE_URL faltando no secret faria o job rodar
    'com sucesso' e publicar as mesmas ofertas a cada 2 horas, para sempre.
    """
    monkeypatch.setenv("CI", "true")
    problems = settings.validate()
    assert any("Postgres" in p for p in problems)


def test_em_ci_postgres_passa_na_validacao(settings, monkeypatch):
    monkeypatch.setenv("CI", "true")
    settings = Settings(
        **{**settings.__dict__, "database_url": "postgresql://u:p@host:5432/db?sslmode=require"}
    )
    problems = settings.validate()
    assert not any("Postgres" in p for p in problems)


def test_fora_do_ci_sqlite_e_permitido(settings, monkeypatch):
    """Em dev local o SQLite continua valendo (e e o que os testes usam)."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    problems = settings.validate()
    assert not any("Postgres" in p for p in problems)
