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
    from telegram import Bot, ParseMode
    from telegram.error import Forbidden, RetryAfter, TelegramError

    TELEGRAM_AVAILABLE = True
except Exception:  # pragma: no cover
    Bot = None  # type: ignore[assignment]
    ParseMode = None  # type: ignore[assignment]
    Forbidden = RetryAfter = TelegramError = Exception  # type: ignore[assignment]
    TELEGRAM_AVAILABLE = False

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 2.0


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
        kwargs: dict[str, Any] = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text,
        }
        if ParseMode is not None:
            kwargs["parse_mode"] = ParseMode.HTML

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await bot.send_message(**kwargs)
                return
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
            except TelegramError as exc:
                wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning("Falha no envio (%s). Nova tentativa em %.1fs", exc, wait)
                await self._sleep(wait)
                last_error = exc
            except Exception as exc:  # rede/socket
                wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning("Erro de rede no envio (%s). Nova tentativa em %.1fs", exc, wait)
                await self._sleep(wait)
                last_error = exc

        raise RuntimeError(f"Falha ao enviar mensagem ao Telegram: {last_error}")

    async def publish_offer(self, offer: Offer) -> bool:
        """Envia uma oferta. Retorna True se foi efetivamente publicada."""
        text = build_message(offer)
        if not self.settings.telegram_enabled:
            log.info("[dry-run] Nao enviado (TELEGRAM_ENABLED=false):\n%s", text)
            return False
        await self._throttle()
        await self._send_text(text)
        log.info("Oferta publicada: %s (%s)", offer.title, offer.store)
        return True

    async def publish(self, offers: list[Offer]) -> int:
        """Publica uma lista de ofertas. Uma falha isolada nao aborta o ciclo."""
        sent = 0
        for offer in offers:
            try:
                if await self.publish_offer(offer):
                    sent += 1
            except Forbidden:
                raise
            except Exception as exc:
                log.error("Falha ao publicar %s: %s", offer.title, exc)
                continue
        return sent
