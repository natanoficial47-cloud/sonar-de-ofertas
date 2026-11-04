"""Diagnostico das fontes: testa credenciais e mostra o que cada loja devolve.

Uso:
    python scripts/check_sources.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from config import Settings  # noqa: E402
from services import AmazonAdapter, MercadoLivreAdapter, ShopeeAdapter  # noqa: E402


async def main() -> int:
    settings = Settings.from_env()
    adapters = [
        AmazonAdapter(settings.amazon),
        ShopeeAdapter(settings.shopee),
        MercadoLivreAdapter(settings.mercadolivre),
    ]

    print(f"Fontes ativas: {', '.join(settings.sources)}\n")
    falhas = 0

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for adapter in adapters:
            nome = adapter.name
            if not adapter.configured:
                print(f"[{nome}] SEM CREDENCIAL -> roda em modo demo")
                continue
            try:
                ofertas = await adapter.fetch(client)
            except Exception as exc:  # noqa: BLE001
                falhas += 1
                print(f"[{nome}] FALHOU: {type(exc).__name__}: {exc}")
                continue

            if not ofertas:
                print(f"[{nome}] Conectou, mas veio nenhuma oferta (revise as keywords)")
            else:
                melhor = max(ofertas, key=lambda o: o.effective_discount)
                print(
                    f"[{nome}] OK - {len(ofertas)} oferta(s). "
                    f"Melhor: {melhor.title[:50]} | "
                    f"{melhor.effective_discount:.0f}% OFF | {melhor.url}"
                )

    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
