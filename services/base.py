"""Contrato comum dos adaptadores de loja."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from models import Offer

log = logging.getLogger(__name__)


class StoreAdapter(ABC):
    name: str = "loja"

    def __init__(self, settings: Any):
        self.settings = settings

    @property
    def configured(self) -> bool:
        """True quando existem credenciais para chamar a API de producao."""
        return False

    @abstractmethod
    async def fetch(self, client: Any) -> list[Offer]:
        """Busca ofertas reais na loja."""

    def mock_offers(self, cycle: int = 0) -> list[Offer]:
        """Ofertas de demonstracao, usadas quando a loja nao esta configurada."""
        from services.mocks import mock_offers_for

        return mock_offers_for(self.name, cycle)

    async def collect(self, client: Any, cycle: int = 0) -> list[Offer]:
        """Ponto de entrada unificado: real quando configurado, demo senao.

        Se a loja esta configurada mas a chamada falha, NAO devolvemos mock:
        publicar produto ficticio num grupo real e pior do que nao publicar.
        """
        if not self.configured:
            offers = self.mock_offers(cycle)
            log.info(
                "[%s] Sem credenciais: usando %d oferta(s) de demonstracao",
                self.name,
                len(offers),
            )
            return offers

        try:
            offers = await self.fetch(client)
            log.info("[%s] %d oferta(s) obtidas da API", self.name, len(offers))
            return offers
        except Exception as exc:
            log.error("[%s] Falha na consulta a API: %s", self.name, exc)
            return []
