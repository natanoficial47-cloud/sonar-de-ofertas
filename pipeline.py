"""Regras de selecao: quem merece ser publicado no grupo."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import Settings
from database import Database
from formatter import brl
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
        if current is None or offer.attractiveness > current.attractiveness:
            best[offer.url_hash] = offer
    return list(best.values())


def _economy_labels(offer: Offer) -> list[str]:
    """Descreve de onde vem o desconto (para o log do ciclo)."""
    labels: list[str] = []
    if offer.effective_discount > 0:
        labels.append(
            f"{offer.effective_discount:.0f}% OFF (de {brl(offer.original_price)})"
        )
    if offer.effective_comparison_pct > 0:
        labels.append(
            f"{offer.effective_comparison_pct:.0f}% abaixo do preço típico "
            f"({brl(offer.comparison_price)})"
        )
    return labels


def _apply_keyword_mix(
    offers: list[Offer], limit_per_keyword: int
) -> tuple[list[Offer], list[Offer]]:
    """Garante variedade de categorias no ciclo.

    Sem isso, o grupo inteiro do dia sai do mesmo nicho (o que tem o maior
    desconto). Com o teto por categoria, cada ciclo entra com ofertas de
    termos de busca diferentes. Ofertas sem categoria (outras lojas) nao
    sao limitadas aqui.
    """
    if limit_per_keyword <= 0:
        return offers, []

    kept: list[Offer] = []
    dropped: list[Offer] = []
    counter: dict[str, int] = {}
    for offer in offers:
        key = (offer.keyword or "").strip().lower()
        if not key:
            kept.append(offer)
            continue
        used = counter.get(key, 0)
        if used >= limit_per_keyword:
            dropped.append(offer)
            continue
        counter[key] = used + 1
        kept.append(offer)
    return kept, dropped


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

        if (
            offer.effective_discount < settings.min_discount_pct
            and offer.effective_comparison_pct < settings.min_comparison_pct
        ):
            result.skipped.append(
                (
                    offer,
                    "desconto insuficiente "
                    f"({offer.effective_discount:.0f}% tabela / "
                    f"{offer.effective_comparison_pct:.0f}% menor preco)",
                )
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

        log.debug(
            "Passou nos filtros: %s (%s) - %s | %s | %s",
            offer.title,
            offer.store,
            reason,
            brl(offer.current_price),
            "; ".join(_economy_labels(offer)) or "sem desconto",
        )
        result.to_send.append(offer)

    # Melhores descontos primeiro, respeitando o teto por ciclo.
    result.to_send.sort(key=lambda o: o.attractiveness, reverse=True)

    # Variedade: depois de ordenar, corta as repetidas da mesma categoria
    # para o ciclo nao ficar monopolizado por um nicho so.
    mixed, dropped = _apply_keyword_mix(
        result.to_send, settings.max_offers_per_keyword_per_cycle
    )
    for offer in dropped:
        result.skipped.append((offer, "categoria ja repetida neste ciclo"))
    result.to_send = mixed

    if len(result.to_send) > settings.max_offers_per_cycle:
        extra = result.to_send[settings.max_offers_per_cycle :]
        result.skipped.extend((offer, "excedeu o limite por ciclo") for offer in extra)
        result.to_send = result.to_send[: settings.max_offers_per_cycle]

    # Log do que realmente vai para o grupo (a lista final, ja filtrada).
    for offer in result.to_send:
        log.info(
            "Selecionada: %s (%s) | %s | %s | %s",
            offer.title,
            offer.store,
            brl(offer.current_price),
            "; ".join(_economy_labels(offer)) or "sem desconto",
            f"[{offer.keyword}]" if offer.keyword else "",
        )

    return result
