"""Motor do bot de ofertas.

Uso:
    python main.py --once     # um ciclo e sai (ideal para cron do GitHub Actions)
    python main.py --daemon   # loop continuo com intervalo configurado
    python main.py --once --dry-run  # nao envia nada, so loga
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from dataclasses import replace
from typing import Any

import httpx

from config import Settings
from database import Database
from models import Offer
from pipeline import select_offers
from services import AmazonAdapter, MercadoLivreAdapter, ShopeeAdapter
from telegram_client import TelegramPublisher

log = logging.getLogger("bot_ofertas")


def setup_logging(level: str) -> None:
    # No console do Windows a saida padrao e cp1252 e os emojis viram lixo.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_adapters(settings: Settings) -> list[Any]:
    all_adapters = [
        AmazonAdapter(settings.amazon),
        ShopeeAdapter(settings.shopee),
        MercadoLivreAdapter(settings.mercadolivre),
    ]
    wanted = {s.strip().lower() for s in settings.sources}
    return [a for a in all_adapters if a.source_key in wanted]


def apply_persisted_secrets(settings: Settings, db: Database) -> Settings:
    """Reaplica segredos guardados no banco (tokens renovados).

    O access_token da Shopee expira em ~4h e o do Mercado Livre em ~6h; guardar
    o token novo no banco evita depender do .env em cada execucao do cron.
    """
    token = db.kv_get("shopee_access_token")
    if token and token != settings.shopee.access_token:
        log.info("Token da Shopee restaurado do banco")
        settings = replace(
            settings,
            shopee=replace(settings.shopee, access_token=token),
        )

    ml_token = db.kv_get("mercadolivre_access_token")
    if ml_token and ml_token != settings.mercadolivre.access_token:
        log.info("Token do Mercado Livre restaurado do banco")
        settings = replace(
            settings,
            mercadolivre=replace(settings.mercadolivre, access_token=ml_token),
        )
    return settings


def persist_refreshed_secrets(adapters: list[Any], db: Database) -> None:
    """Grava tokens renovados durante o ciclo."""
    for adapter in adapters:
        token = getattr(adapter, "refreshed_access_token", None)
        if not token:
            continue
        if adapter.name == "Shopee":
            db.kv_set("shopee_access_token", token)
            log.info("Token da Shopee salvo no banco")
        elif adapter.name == "Mercado Livre":
            db.kv_set("mercadolivre_access_token", token)
            log.info("Token do Mercado Livre salvo no banco")


async def fetch_all(adapters: list[Any], cycle: int) -> list[Offer]:
    """Consulta todas as lojas em paralelo; uma falha nao derruba as demais."""
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(adapter.collect(client, cycle) for adapter in adapters),
            return_exceptions=True,
        )

    offers: list[Offer] = []
    for adapter, result in zip(adapters, results):
        if isinstance(result, Exception):
            log.error("[%s] Falha inesperada: %s", adapter.name, result)
            continue
        offers.extend(result)
    return offers


async def run_cycle(
    settings: Settings,
    db: Database,
    cycle: int = 0,
    publisher: TelegramPublisher | None = None,
    adapters: list[Any] | None = None,
) -> dict:
    """Executa um ciclo completo: coleta, filtra, publica e registra."""
    run_id = db.start_run()
    sent = 0
    found = 0
    error: str | None = None
    status = "ok"

    try:
        adapters = adapters if adapters is not None else build_adapters(settings)
        offers = await fetch_all(adapters, cycle)
        found = len(offers)
        log.info("Ciclo %d: %d oferta(s) bruta(s) coletada(s)", cycle, found)

        persist_refreshed_secrets(adapters, db)

        for offer in offers:
            db.record_seen(offer)

        selection = select_offers(offers, db, settings)
        for offer, reason in selection.skipped:
            log.debug("Descartada: %s (%s)", offer.title, reason)

        publisher = publisher or TelegramPublisher(settings)
        sent_offers = await publisher.publish(selection.to_send)
        sent = len(sent_offers)

        # So marca como enviado o que realmente saiu: em dry-run ou em falha
        # de envio, a oferta continua pendente para o proximo ciclo.
        for offer in sent_offers:
            db.mark_sent(offer)

        log.info(
            "Ciclo %d concluido: %d encontrada(s), %d aprovada(s), %d enviada(s)",
            cycle,
            found,
            len(selection.to_send),
            sent,
        )
    except Exception as exc:  # noqa: BLE001 - o ciclo nunca pode matar o processo
        status = "erro"
        error = str(exc)
        log.exception("Falha no ciclo %d: %s", cycle, exc)
    finally:
        db.finish_run(run_id, status=status, offers_found=found, offers_sent=sent, error=error)

    return {"found": found, "sent": sent, "status": status, "error": error}


async def run_daemon(settings: Settings) -> int:
    """Loop continuo, com shutdown gracioso em SIGINT/SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(*_: Any) -> None:
        log.info("Sinal de encerramento recebido, finalizando...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):  # pragma: no cover (Windows)
            pass

    db = Database(settings.database_url).connect()
    settings = apply_persisted_secrets(settings, db)
    cycle = int(db.kv_get("cycle_counter") or 0)
    try:
        while not stop.is_set():
            await run_cycle(settings, db, cycle)
            cycle += 1
            db.kv_set("cycle_counter", str(cycle))
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        db.close()
    return 0


async def run_once(settings: Settings) -> int:
    db = Database(settings.database_url).connect()
    settings = apply_persisted_secrets(settings, db)
    try:
        # O contador de ciclo vive no banco: no cron cada execucao e um
        # processo novo, e sem isso a rotacao de produtos do Mercado Livre
        # (que evita repetir as mesmas ofertas) sempre comecaria do zero.
        cycle = int(db.kv_get("cycle_counter") or 0)
        result = await run_cycle(settings, db, cycle=cycle)
        db.kv_set("cycle_counter", str(cycle + 1))
    finally:
        db.close()
    return 0 if result["status"] == "ok" else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bot de ofertas para Telegram")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="roda um unico ciclo e sai")
    mode.add_argument("--daemon", action="store_true", help="roda em loop continuo")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="nao envia ao Telegram, apenas registra o que seria enviado",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.from_env()
    setup_logging(settings.log_level)

    if args.dry_run:
        settings = Settings(**{**settings.__dict__, "telegram_enabled": False})
        log.info("Modo dry-run: nenhuma mensagem sera enviada")

    problems = settings.validate()
    if problems:
        for problem in problems:
            log.error("Configuracao: %s", problem)
        log.error(
            "Ajuste as variaveis de ambiente (veja .env.example) ou rode com --dry-run. "
            "As ofertas de demo so comecam a ser enviadas se POST_DEMO_OFFERS=true."
        )
        return 2

    try:
        if args.daemon:
            return asyncio.run(run_daemon(settings))
        return asyncio.run(run_once(settings))
    except KeyboardInterrupt:  # pragma: no cover
        log.info("Interrompido pelo usuario")
        return 0


if __name__ == "__main__":
    sys.exit(main())
