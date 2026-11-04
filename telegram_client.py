"""Publicacao no Telegram, com retentativa, rate limit e modo dry-run."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import Settings
from formatter import build_message
from models import Offer

log = logging.getLogger(__name__)

try:  # pragma: no cover - dependencia opcional em ambiente de teste
    from telegram import Bot
    from telegram.error import (
        BadRequest,
        Forbidden,
        NetworkError,
        RetryAfter,
        TelegramError,
    )

    try:
        # PTB 22+ moveu ParseMode para telegram.constants (o top-level sumiu).
        from telegram.constants import ParseMode
    except ImportError:  # pragma: no cover - PTB 21
        from telegram import ParseMode

    try:
        from telegram.error import TimedOut
    except ImportError:  # pragma: no cover - PTB antigo
        TimedOut = TimeoutError  # type: ignore[assignment,misc]

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx e dependencia do PTB
        httpx = None  # type: ignore[assignment]

    TELEGRAM_AVAILABLE = True
except Exception:  # pragma: no cover
    Bot = None  # type: ignore[assignment]
    ParseMode = "HTML"  # type: ignore[assignment]

    # Classes proprias: se usassemos Exception como alias, qualquer erro
    # seria capturado pelos blocos de RetryAfter/Forbidden e mascarado.
    class Forbidden(Exception):  # type: ignore[no-redef]
        pass

    class BadRequest(Exception):  # type: ignore[no-redef]
        pass

    class NetworkError(Exception):  # type: ignore[no-redef]
        pass

    class RetryAfter(Exception):  # type: ignore[no-redef]
        pass

    class TelegramError(Exception):  # type: ignore[no-redef]
        pass

    TimedOut = TimeoutError  # type: ignore[assignment,misc]
    httpx = None  # type: ignore[assignment]
    TELEGRAM_AVAILABLE = False

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 2.0
# O PTB usa timeout de leitura de 5s por padrao -- curto demais para baixar
# a foto do produto no CDN do Mercado Livre. O estouro desse timeout era a
# causa das ofertas duplicadas no grupo (o retry reenviava o que ja tinha
# sido entregue).
CONNECT_TIMEOUT_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 120.0
WRITE_TIMEOUT_SECONDS = 120.0


class DeliveryUncertain(Exception):
    """A requisicao saiu, mas a resposta nao chegou.

    O Telegram pode ter entregado a mensagem. Reenviar (ou mandar o texto
    depois da foto) publicaria a mesma oferta duas vezes no grupo, entao
    esse caso e tratado como "ja enviada".
    """


class TelegramPublisher:
    """Envia ofertas para o grupo configurado.

    O `bot` pode ser injetado (usado nos testes) para nao exigir rede nem
    token valido.
    """

    def __init__(
        self,
        settings: Settings,
        bot: Any = None,
        sleep: Any = asyncio.sleep,
    ):
        self.settings = settings
        self._sleep = sleep
        self._bot = bot
        self._last_send_at = 0.0

    # --------------------------------------------------------------- setup
    def _ensure_bot(self) -> Any:
        if self._bot is None:
            if not TELEGRAM_AVAILABLE:
                raise RuntimeError(
                    "python-telegram-bot nao instalado. Rode: pip install -r requirements.txt"
                )
            if not self.settings.telegram_token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN nao configurado")
            try:
                from telegram.request import HTTPXRequest

                request = HTTPXRequest(
                    connect=CONNECT_TIMEOUT_SECONDS,
                    read=READ_TIMEOUT_SECONDS,
                    write=WRITE_TIMEOUT_SECONDS,
                    pool=CONNECT_TIMEOUT_SECONDS,
                )
                self._bot = Bot(token=self.settings.telegram_token, request=request)
            except Exception:  # pragma: no cover - versao antiga do PTB
                self._bot = Bot(token=self.settings.telegram_token)
        return self._bot

    # -------------------------------------------------------------- envio
    async def _throttle(self) -> None:
        """Garante o intervalo minimo entre duas mensagens no mesmo grupo."""
        delay = max(0.0, self.settings.send_delay_seconds)
        if delay <= 0:
            return
        now = asyncio.get_running_loop().time()
        elapsed = now - self._last_send_at
        if elapsed < delay:
            await self._sleep(delay - elapsed)
        self._last_send_at = asyncio.get_running_loop().time()

    async def _send_text(self, text: str) -> None:
        bot = self._ensure_bot()

        async def call() -> None:
            await bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=text,
                parse_mode=getattr(ParseMode, "HTML", "HTML"),
            )

        await self._deliver(call, "mensagem")

    async def _send_photo(self, photo: str, caption: str) -> None:
        """Envia a foto do produto com a oferta como legenda."""
        bot = self._ensure_bot()
        if not hasattr(bot, "send_photo"):
            raise RuntimeError("send_photo indisponivel nesta versao do bot")

        async def call() -> None:
            await bot.send_photo(
                chat_id=self.settings.telegram_chat_id,
                photo=photo,
                caption=caption,
                parse_mode=getattr(ParseMode, "HTML", "HTML"),
            )

        await self._deliver(call, "foto")

    async def _deliver(self, call: Any, what: str) -> None:
        """Executa o envio com retentativa, espera e backoff.

        Ordem dos `except` importa: no PTB, `TimedOut` e subclasse de
        `NetworkError`, e `BadRequest` e subclasse de `TelegramError`. Se o
        timeout caísse no bloco generico, o retry reenviaria a mensagem que o
        Telegram ja tinha entregado -- exatamente o bug de oferta duplicada.
        """
        last_error: Exception | None = None
        timeout_types: tuple[type[BaseException], ...] = (
            TimedOut,
            asyncio.TimeoutError,
        )
        if httpx is not None:
            timeout_types = timeout_types + (httpx.TimeoutException,)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await call()
                return
            except timeout_types as exc:
                # A requisicao saiu, mas a resposta nao chegou: o Telegram
                # pode ter entregado a mensagem. Nao reenviar.
                raise DeliveryUncertain(
                    f"Timeout ao enviar {what}: o Telegram pode ter entregado a mensagem"
                ) from exc
            except RetryAfter as exc:
                wait = float(getattr(exc, "retry_after", BASE_BACKOFF_SECONDS))
                log.warning("Telegram pediu para aguardar %.1fs (tentativa %d)", wait, attempt)
                await self._sleep(wait + 0.5)
                last_error = exc
            except Forbidden as exc:
                log.error(
                    "Telegram recusou a mensagem (bot sem permissao ou bloqueado): %s", exc
                )
                raise
            except BadRequest as exc:
                # Erro definitivo (HTML invalido, chat invalido, ...):
                # repetir nao resolve.
                log.error("Telegram recusou o conteudo do envio: %s", exc)
                raise
            except (NetworkError, TelegramError) as exc:
                wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning("Falha no envio (%s). Nova tentativa em %.1fs", exc, wait)
                await self._sleep(wait)
                last_error = exc
            except Exception as exc:  # rede/socket
                wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning("Erro de rede no envio (%s). Nova tentativa em %.1fs", exc, wait)
                await self._sleep(wait)
                last_error = exc

        raise RuntimeError(f"Falha ao enviar {what} ao Telegram: {last_error}")

    async def publish_offer(self, offer: Offer) -> bool:
        """Envia uma oferta. Retorna True se foi efetivamente publicada."""
        text = build_message(offer)
        if not self.settings.telegram_enabled:
            log.info("[dry-run] Nao enviado (TELEGRAM_ENABLED=false):\n%s", text)
            return False
        await self._throttle()

        # Foto quando houver: o grupo recebe a imagem do produto com a
        # legenda da oferta. Se a foto falhar, o texto vai do mesmo jeito.
        if offer.image_url:
            try:
                await self._send_photo(offer.image_url, text)
                log.info(
                    "Oferta publicada com foto: %s (%s)", offer.title, offer.store
                )
                return True
            except Forbidden:
                raise
            except DeliveryUncertain:
                # O Telegram pode ter entregado a foto. Mandar o texto agora
                # publicaria a mesma oferta duas vezes no grupo, entao
                # tratamos como enviada (sem repetir no proximo ciclo).
                log.warning(
                    "Timeout na foto de %s: envio pode ter chegado ao grupo. "
                    "Marcando como enviada para nao duplicar.",
                    offer.title,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Nao consegui enviar a foto de %s (%s); mando so o texto",
                    offer.title,
                    exc,
                )

        try:
            await self._send_text(text)
        except DeliveryUncertain:
            log.warning(
                "Timeout no texto de %s: envio pode ter chegado ao grupo. "
                "Marcando como enviada para nao duplicar.",
                offer.title,
            )
            return True
        log.info("Oferta publicada: %s (%s)", offer.title, offer.store)
        return True

    async def publish(self, offers: list[Offer]) -> list[Offer]:
        """Publica uma lista de ofertas e devolve as que foram realmente enviadas.

        Uma falha isolada nao aborta o ciclo. E o retorno importa: so o que
        saiu de verdade pode ser marcado como enviado no banco, senao a
        anti-duplicidade passa a ignorar ofertas que o grupo nunca viu.
        """
        sent: list[Offer] = []
        for offer in offers:
            try:
                if await self.publish_offer(offer):
                    sent.append(offer)
            except Forbidden:
                raise
            except Exception as exc:
                log.error("Falha ao publicar %s: %s", offer.title, exc)
                continue
        return sent
