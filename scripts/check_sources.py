"""Diagnostico das fontes: testa credenciais e mostra o que cada loja devolve.

Uso:
    python scripts/check_sources.py          # varre o catalogo (gasto, mas completo)
    python scripts/check_sources.py --ping   # 1 request por loja (preflight do CI)

O `--ping` existe para o deploy: antes de rodar o ciclo que publica no
grupo, ele confirma que as credenciais estao valendo de verdade. Sem
credencial a loja entra em modo demo (e nada ficticio e publicado, porque
`POST_DEMO_OFFERS=false`).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from config import Settings  # noqa: E402
from services import AmazonAdapter, MercadoLivreAdapter, ShopeeAdapter  # noqa: E402


async def run_ping(settings: Settings) -> int:
    """Teste rapido: uma requisicao autenticada por loja."""
    adapters = [
        AmazonAdapter(settings.amazon),
        ShopeeAdapter(settings.shopee),
        MercadoLivreAdapter(settings.mercadolivre),
    ]
    falhas = 0
    print("Preflight das fontes (1 request por loja):\n")

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for adapter in adapters:
            if not adapter.configured:
                print(f"[{adapter.name}] SEM CREDENCIAL -> modo demo (nada e publicado)")
                continue
            try:
                detalhe = await adapter.ping(client)
            except Exception as exc:  # noqa: BLE001
                falhas += 1
                print(f"[{adapter.name}] FALHOU: {type(exc).__name__}: {exc}")
                continue
            print(f"[{adapter.name}] OK - {detalhe}")

    # Nenhuma loja configurada = nada sera publicado, e o job "verde" seria
    # enganoso: o grupo simplesmente nao receberia ofertas. Falha aqui.
    if not any(a.configured for a in adapters):
        print(
            "\nNENHUMA loja configurada -- nada sera publicado.\n"
            "Cadastre as credenciais nos secrets do repositorio "
            "(MERCADOLIVRE_CLIENT_ID / MERCADOLIVRE_CLIENT_SECRET)."
        )
        return 1

    if falhas:
        print(f"\n{falhas} fonte(s) com credencial quebrada.")
    return 1 if falhas else 0


async def run_full(settings: Settings) -> int:
    """Varredura completa: coleta ofertas de verdade em cada loja."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostico das fontes de ofertas")
    parser.add_argument(
        "--ping",
        action="store_true",
        help="teste rapido (1 request por loja) em vez da varredura completa",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.ping:
        return asyncio.run(run_ping(settings))
    return asyncio.run(run_full(settings))


if __name__ == "__main__":
    sys.exit(main())
