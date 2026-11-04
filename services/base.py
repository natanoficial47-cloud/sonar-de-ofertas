"""Contrato comum dos adaptadores de loja."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from models import Offer

log = logging.getLogger(__name__)


class StoreAdapter(ABC):
    name: str = "loja"
    # Chave usada em OFFERS_SOURCES (sem espacos, minuscula).
    key: str = ""

    def __init__(self, settings: Any):
        self.settings = settings
        # Numero do ciclo atual: permite variar a coleta entre execucoes
        # (o cron roda de 2 em 2 horas e precisa achar coisa nova).
        self.cycle = 0

    @property
    def source_key(self) -> str:
        """Identificador da loja em OFFERS_SOURCES."""
        return self.key or self.name.lower().replace(" ", "")

    @property
    def configured(self) -> bool:
        """True quando existem credenciais para chamar a API de producao."""
        return False

    @abstractmethod
    async def fetch(self, client: Any) -> list[Offer]:
        """Busca ofertas reais na loja."""

    async def ping(self, client: Any) -> str:
        """Teste rapido de credencial: 1 request, sem varrer o catalogo.

        Usado pelo preflight do deploy (`check_sources.py --ping`) para
        descobrir credencial quebrada *antes* de rodar o ciclo completo.
        Lojas que nao implementam o teste retornam uma mensagem explicativa.
        """
        return "sem teste rapido implementado"

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
            self.cycle = cycle
            offers = await self.fetch(client)
            log.info("[%s] %d oferta(s) obtidas da API", self.name, len(offers))
            return offers
        except Exception as exc:
            log.error("[%s] Falha na consulta a API: %s", self.name, exc)
            return []
