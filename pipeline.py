"""Regras de selecao: quem merece ser publicado no grupo."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import Settings
from database import Database
from models import Offer

log = logging.getLogger(__name__)


@dataclass
class Selection:
    to_send: list[Offer] = field(default_factory=list)
    skipped: list[tuple[Offer, str]] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _dedupe(offers: list[Offer]) -> list[Offer]:
    """Mantem a melhor oferta por hash de URL dentro do proprio ciclo."""
    best: dict[str, Offer] = {}
    for offer in offers:
        current = best.get(offer.url_hash)
        if current is None or offer.effective_discount > current.effective_discount:
            best[offer.url_hash] = offer
    return list(best.values())


def select_offers(
    offers: list[Offer],
    db: Database,
    settings: Settings,
    now: int | None = None,
) -> Selection:
    """Aplica os filtros de negocio e a regra de anti-duplicidade."""
    now = now or int(time.time())
    result = Selection()

    for offer in _dedupe(offers):
        if offer.is_demo and not settings.post_demo_offers:
            result.skipped.append((offer, "oferta de demo desativada"))
            continue

        if offer.current_price < settings.min_price:
            result.skipped.append((offer, f"preco abaixo do minimo ({offer.current_price:.2f})"))
            continue

        if offer.current_price > settings.max_price:
            result.skipped.append((offer, f"preco acima do maximo ({offer.current_price:.2f})"))
            continue

        if offer.effective_discount < settings.min_discount_pct:
            result.skipped.append(
                (offer, f"desconto insuficiente ({offer.effective_discount:.0f}%)")
            )
            continue

        send, reason = db.should_send(
            offer,
            now=now,
            cooldown_hours=settings.resend_cooldown_hours,
            price_drop_retrigger_pct=settings.price_drop_retrigger_pct,
        )
        if not send:
            result.skipped.append((offer, reason))
            continue

        log.info("Aprovada: %s (%s) - %s", offer.title, offer.store, reason)
        result.to_send.append(offer)

    # Melhores descontos primeiro, respeitando o teto por ciclo.
    result.to_send.sort(key=lambda o: o.effective_discount, reverse=True)
    if len(result.to_send) > settings.max_offers_per_cycle:
        extra = result.to_send[settings.max_offers_per_cycle :]
        result.skipped.extend((offer, "excedeu o limite por ciclo") for offer in extra)
        result.to_send = result.to_send[: settings.max_offers_per_cycle]

    return result
