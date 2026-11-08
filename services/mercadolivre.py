"""Mercado Livre.

Credenciais:
  - `MERCADOLIVRE_ACCESS_TOKEN` pronto: usado direto.
  - ou `MERCADOLIVRE_CLIENT_ID` + `MERCADOLIVRE_CLIENT_SECRET`: o proprio
    adapter troca por um access token via `POST /oauth/token`
    (`grant_type=client_credentials`). O token vive ~6h, entao a renovacao
    acontece sozinha e o token novo e guardado no banco.

O app deste projeto so tem os recursos **Busca** (`/products/search`) e
**Itens** (`/products/{id}/items`) liberados. A busca classica
(`/sites/MLB/search`) e o detalhe do anuncio (`/items/{id}`) respondem 403
`forbidden` ate o app ser aprovado no painel do Mercado Livre.

Por isso o desconto vem de duas fontes honestas:
  1. `original_price` do anuncio (preco de tabela) -> desconto real.
  2. menor preco entre as ofertas do mesmo produto de catalogo -> o anuncio
     e o mais barato entre N vendedores do mesmo produto.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import MercadoLivreSettings
from models import Offer
from services.base import StoreAdapter

log = logging.getLogger(__name__)

API_BASE = "https://api.mercadolibre.com"
OAUTH_URL = f"{API_BASE}/oauth/token"
WEB_BASE = "https://www.mercadolivre.com.br"


def _shipping_info(item: dict) -> tuple[bool, float]:
    """(frete gratis, custo do frete) de um anuncio.

    O ML nem sempre marca a flag `free_shipping`, mas um custo de frete 0
    significa, na pratica, frete gratis -- e e o preco final que a pagina
    mostra. Anuncio sem o bloco `shipping` fica com custo 0 (sem informacao).
    """
    shipping = item.get("shipping")
    if not isinstance(shipping, dict) or not shipping:
        return False, 0.0
    if shipping.get("free_shipping"):
        return True, 0.0
    try:
        cost = float(shipping.get("cost") or 0)
    except (TypeError, ValueError):
        return False, 0.0
    if cost <= 0:
        return True, 0.0
    return False, cost


def _total_price(item: dict) -> float:
    """Preco que o comprador paga de verdade: produto + frete."""
    price = float(item.get("price") or 0)
    free, cost = _shipping_info(item)
    return price if free else price + cost


def _median(values: list[float]) -> float:
    """Mediana: o preco tipico do conjunto (nada de outlier inflando)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _confirms_table_price(item: dict, table_price: float) -> bool:
    """Outro vendedor do mesmo produto confirma o preco de tabela.

    Serve como **corroboracao** (nao como exigencia): quando existe, a
    mensagem pode dizer que o preco de tabela tambem aparece no mercado.
    Exige que o vendedor cobre pelo menos 70% do preco de tabela e que nao
    esteja ele mesmo em promocao.
    """
    price = float(item.get("price") or 0)
    if price <= 0 or price < table_price * 0.7:
        return False
    own_table = item.get("original_price")
    if own_table:
        try:
            if price < float(own_table) * 0.9:
                return False  # o proprio vendedor esta em promocao
        except (TypeError, ValueError):
            return False
    return True


async def fetch_access_token(
    client: Any, client_id: str, client_secret: str
) -> str:
    """Troca client_id + client_secret por um access token."""
    response = await client.post(
        OAUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    payload = response.json()
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(
            "Mercado Livre nao devolveu access_token: "
            f"{payload.get('message') or payload.get('error') or payload}"
        )
    log.info(
        "Access token do Mercado Livre obtido (validade %ss)",
        payload.get("expires_in"),
    )
    return token


class MercadoLivreAdapter(StoreAdapter):
    name = "Mercado Livre"
    key = "mercadolivre"

    def __init__(self, settings: MercadoLivreSettings):
        super().__init__(settings)
        self.cfg = settings
        # Token obtido/renovado durante o ciclo (o main persiste no banco).
        self.refreshed_access_token: str | None = None

    @property
    def configured(self) -> bool:
        # Precisa de access token OU de credenciais para obter um.
        return bool(self.cfg.access_token) or bool(
            self.cfg.client_id and self.cfg.client_secret
        )

    async def _ensure_token(self, client: Any, force: bool = False) -> str | None:
        """Devolve um access token valido, obtendo/renovando se preciso.

        `force=True` ignora o token em cache: o token do ML vive ~6h, e o
        que ficou salvo no banco pode estar vencido (a API responde 401).
        """
        if not force and self.cfg.access_token:
            return self.cfg.access_token
        if not (self.cfg.client_id and self.cfg.client_secret):
            return None
        try:
            token = await fetch_access_token(
                client, self.cfg.client_id, self.cfg.client_secret
            )
        except Exception as exc:  # noqa: BLE001
            log.error("[Mercado Livre] Falha ao obter access token: %s", exc)
            return None
        self.refreshed_access_token = token
        return token

    # ------------------------------------------------------------------
    # Coleta
    # ------------------------------------------------------------------

    async def fetch(self, client: Any) -> list[Offer]:
        token = await self._ensure_token(client)
        if not token:
            log.error("[Mercado Livre] Sem access token: nada a consultar")
            return []

        headers = {
            "Accept": "application/json",
            "User-Agent": "bot-ofertas-telegram/1.0",
            "Authorization": f"Bearer {token}",
        }

        # 1) Produtos candidatos de cada keyword (paginado).
        candidates = await self._collect_candidates(client, headers)

        # 2) Sondagem dos itens (gasta orçamento e respeita rate limit).
        offers = await self._probe_items(client, headers, candidates)

        log.info("[Mercado Livre] %d oferta(s) montada(s)", len(offers))
        return offers

    async def ping(self, client: Any) -> str:
        """Um GET de busca (1 resultado) prova que o token e valido."""
        token = await self._ensure_token(client)
        if not token:
            raise RuntimeError("sem access token (falha ao obter)")
        response = await client.get(
            f"{API_BASE}/products/search",
            params={
                "q": self.cfg.keywords[0] if self.cfg.keywords else "fone",
                "site_id": self.cfg.site_id,
                "status": "active",
                "limit": 1,
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return "token valido, busca respondendo"

    async def _collect_candidates(
        self, client: Any, headers: dict
    ) -> list[tuple[str, dict]]:
        """Busca produtos de catalogo em varias paginas, sem repetir produto."""
        seen: set[str] = set()
        by_keyword: dict[str, list[dict]] = {k: [] for k in self.cfg.keywords}

        for keyword in self.cfg.keywords:
            for offset in self.cfg.search_offsets:
                params = {
                    "q": keyword,
                    "site_id": self.cfg.site_id,
                    "status": "active",
                    "limit": self.cfg.search_limit,
                    "offset": offset,
                }
                response = await self._get_with_retry(
                    client, f"{API_BASE}/products/search", params, headers
                )
                if response is None:
                    continue
                if response.status_code == 400:
                    # A janela de resultados tem limite (offset + limit <= 200).
                    log.info(
                        "[Mercado Livre] Fim da paginacao de %s (offset %s)",
                        keyword,
                        offset,
                    )
                    break
                if response.status_code != 200:
                    log.info(
                        "[Mercado Livre] Busca %s (offset %s) -> %s",
                        keyword,
                        offset,
                        response.status_code,
                    )
                    continue

                results = response.json().get("results") or []
                if not results:
                    break  # fim da paginacao
                for product in results:
                    product_id = product.get("catalog_product_id") or product.get("id")
                    if not product_id or product_id in seen:
                        continue
                    seen.add(product_id)
                    by_keyword[keyword].append(product)

        # Intercala as keywords: o orcamento de sondas precisa ser dividido
        # entre elas, senao a primeira keyword consome tudo.
        candidates: list[tuple[str, dict]] = []
        rounds = max((len(v) for v in by_keyword.values()), default=0)
        for index in range(rounds):
            for keyword, products in by_keyword.items():
                if index < len(products):
                    candidates.append((keyword, products[index]))

        log.info(
            "[Mercado Livre] %d produto(s) candidato(s) na busca (%s)",
            len(candidates),
            ", ".join(f"{k}: {len(v)}" for k, v in by_keyword.items()),
        )
        return candidates

    async def _probe_items(
        self, client: Any, headers: dict, candidates: list[tuple[str, dict]]
    ) -> list[Offer]:
        """Consulta os anuncios de cada produto e monta as ofertas."""
        offers: list[Offer] = []
        seen_items: set[str] = set()
        budget = self.cfg.max_item_probes

        # A cada ciclo a sondagem comeca em um ponto diferente da lista: o
        # catalogo do ML e o mesmo, mas o orcamento de requests e limitado.
        if candidates:
            start = (self.cycle * self.cfg.max_item_probes) % len(candidates)
            candidates = candidates[start:] + candidates[:start]

        for keyword, product in candidates:
            if budget <= 0:
                log.info("[Mercado Livre] Orcamento de sondas esgotado")
                break
            budget -= 1

            product_id = product.get("catalog_product_id") or product.get("id")
            response = await self._get_with_retry(
                client,
                f"{API_BASE}/products/{product_id}/items",
                {"limit": self.cfg.max_items_per_product},
                headers,
            )
            if response is None:
                continue
            if response.status_code != 200:
                # 404 "No winners found": o produto nao expoe anuncios.
                continue

            items = response.json().get("results") or []
            if not items:
                continue

            for offer in self._offers_from_items(product, items, keyword):
                if offer.external_id in seen_items:
                    continue
                seen_items.add(offer.external_id)
                offers.append(offer)

            if self.cfg.request_delay_seconds:
                await asyncio.sleep(self.cfg.request_delay_seconds)

        return offers

    def _offers_from_items(
        self, product: dict, items: list[dict], keyword: str | None = None
    ) -> list[Offer]:
        """Monta a oferta de um produto de catalogo.

        Escolha do anuncio: o **melhor preco final** (produto + frete), dando
        preferencia ao que tem frete gratis. Escolher so pelo menor preco do
        produto era o erro que publicava "R$ 29,99" para um anuncio que
        custava R$ 76,83 com o frete -- o grupo via um preco que nao existia
        na pagina.

        Desconto: so entra no que da para provar.
          - `original_price` (preco de tabela) do **anuncio que vai no link**:
            e o valor riscado na pagina do proprio item (confirmado na PDP).
            Vira "% OFF" direto.
          - sem preco de tabela, o desconto vem do menor preco do produto
            frente ao **preco tipico** (mediana) dos outros vendedores --
            comparar com o mais caro inflaria o numero.
        """
        catalog_id = str(product.get("catalog_product_id") or product.get("id") or "")
        name = str(product.get("name") or "Produto")
        picture = None
        pictures = product.get("pictures") or []
        if pictures and isinstance(pictures[0], dict):
            picture = pictures[0].get("url")

        usable = [
            item
            for item in items
            if item.get("item_id") and item.get("price") is not None
        ]
        if not usable:
            return []

        # O Mercado Livre destaca o anuncio com frete gratis, e esse e o
        # preco que a pagina mostra. Compara so entre eles.
        free_items = [item for item in usable if _shipping_info(item)[0]]
        pool = free_items or usable
        cheapest = min(pool, key=lambda item: (_total_price(item), float(item["price"])))
        cheapest_price = float(cheapest["price"])

        # "Preco tipico" do produto: mediana dos anuncios da mesma base de
        # comparacao (quando ha frete gratis, so os anuncios com frete
        # gratis -- que sao os que a pagina destaca).
        typical_price = _median([float(item["price"]) for item in pool])

        table_price = cheapest.get("original_price")
        table_price = float(table_price) if table_price else None
        others = [item for item in usable if float(item["price"]) > cheapest_price]
        # Corroboracao (opcional): outro vendedor do mesmo produto cobra
        # perto do mesmo preco de tabela.
        table_corroborated = bool(
            table_price
            and table_price > cheapest_price
            and any(_confirms_table_price(item, table_price) for item in others)
        )

        offer = self._build_offer(
            cheapest,
            product,
            name=name,
            catalog_id=catalog_id,
            image=picture,
            typical_price=typical_price,
            total_items=len(pool),
            table_corroborated=table_corroborated,
            keyword=keyword,
        )
        return [offer] if offer is not None else []

    def _build_offer(
        self,
        item: dict,
        product: dict,
        *,
        name: str,
        catalog_id: str,
        image: str | None,
        typical_price: float,
        total_items: int,
        table_corroborated: bool,
        keyword: str | None = None,
    ) -> Offer | None:
        item_id = item.get("item_id")
        price = item.get("price")
        if not item_id or price is None:
            return None

        price = float(price)
        original = item.get("original_price")
        original_price = float(original) if original else None

        # O `original_price` do anuncio e o preco de tabela do proprio item
        # (o valor riscado na pagina). Como o link leva o `item_id` desse
        # anuncio, o desconto anunciado bate com o que a pagina mostra.
        discount_pct = 0.0
        if original_price and original_price > price:
            discount_pct = round((1 - price / original_price) * 100, 2)

        # Comparacao com o preco tipico (mediana) dos outros vendedores
        # (usada quando o anuncio nao tem preco de tabela).
        comparison_price = None
        comparison_count = 0
        if not discount_pct and total_items > 1 and typical_price > price:
            # Sanidade: se o mais barato custa uma fracao do preco tipico, nao
            # e desconto e sim outro produto (condicao/importado). Aí nao
            # entra a comparacao -- sem desconto, a oferta nem e aprovada.
            ratio_floor = self.cfg.comparison_min_ratio
            if ratio_floor <= 0 or price >= typical_price * ratio_floor:
                comparison_price = typical_price
                comparison_count = total_items

        shipping = item.get("shipping")
        if isinstance(shipping, dict) and shipping:
            free_shipping, shipping_cost = _shipping_info(item)
        else:
            # Sem informacao de frete: nao afirma nem gratis nem caro.
            free_shipping, shipping_cost = False, None

        return Offer(
            store=self.name,
            external_id=str(item_id),
            # Identidade do PRODUTO de catalogo: varios anuncios (item_id
            # diferentes) pertencem ao mesmo produto. E por ela que a
            # anti-duplicidade reconhece que o produto ja foi enviado.
            product_id=str(catalog_id) if catalog_id else str(item_id),
            title=name,
            url=self._product_url(catalog_id, str(item_id)),
            current_price=round(price, 2),
            original_price=round(original_price, 2) if original_price else None,
            discount_pct=discount_pct,
            image_url=image,
            coupon=None,
            shipping_cost=None if free_shipping else shipping_cost,
            free_shipping=free_shipping,
            comparison_price=round(comparison_price, 2) if comparison_price else None,
            comparison_count=comparison_count,
            table_corroborated=table_corroborated,
            keyword=keyword,
        )

    def _product_url(self, catalog_id: str, item_id: str) -> str:
        """URL de PDP do anuncio (o formato /p/ resolve no site; o slug, nao)."""
        url = f"{WEB_BASE}/p/{catalog_id}"
        params = {"item_id": item_id}
        if self.cfg.affiliate_id:
            params["tag"] = self.cfg.affiliate_id
        return url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    # ------------------------------------------------------------------
    # HTTP com backoff
    # ------------------------------------------------------------------

    async def _get_with_retry(
        self, client: Any, url: str, params: dict, headers: dict
    ) -> Any:
        """GET com espera entre tentativas (a API responde 429 com tudo junto).

        Um 401 significa token vencido: o adapter troca por um novo e repete
        a mesma chamada uma vez.
        """
        attempts = max(1, self.cfg.max_retries)
        renewed = False
        for attempt in range(attempts):
            try:
                response = await client.get(url, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001
                log.warning("[Mercado Livre] Erro de rede em %s: %s", url, exc)
                response = None

            if response is None:
                await asyncio.sleep(self.cfg.retry_backoff_seconds * (attempt + 1))
                continue

            if response.status_code == 401 and not renewed:
                renewed = True
                token = await self._ensure_token(client, force=True)
                if token:
                    log.info("[Mercado Livre] Token expirado, trocando por um novo")
                    headers["Authorization"] = f"Bearer {token}"
                    continue

            if response.status_code == 429:
                wait = self.cfg.retry_backoff_seconds * (attempt + 1)
                log.info(
                    "[Mercado Livre] Rate limit (429), aguardando %.1fs", wait
                )
                await asyncio.sleep(wait)
                continue

            return response

        return None
