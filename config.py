"""Configuracao central do bot, carregada de variaveis de ambiente / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # pragma: no cover - dotnet opcional em ambiente de teste
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

_TRUE = {"1", "true", "yes", "on", "sim", "y"}


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return raw.strip() if raw else default


def _keywords(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _str(name)
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _is_ci() -> bool:
    """True quando o processo roda em CI (GitHub Actions, GitLab CI...)."""
    raw = os.getenv("CI", "")
    return raw.strip().lower() in _TRUE or bool(os.getenv("GITHUB_ACTIONS"))


# Nichos buscados no Mercado Livre. O grupo precisa misturar categorias --
# nao basta a categoria que tem o maior desconto. Os termos sao genericos
# de proposito: o marketplace responde bem a "air fryer" e a "tenis", mas
# "cadeira gamer executiva com massageador" nao acha nada.
DEFAULT_ML_KEYWORDS: tuple[str, ...] = (
    # tech
    "fone bluetooth",
    "smartwatch",
    "cadeira gamer",
    "notebook",
    "monitor gamer",
    "mouse gamer rgb",
    "teclado gamer",
    "microfone condenser",
    "relogio masculine",
    "furadeira de impacto",
    "ssd externo",
    # casa e cozinha
    "air fryer",
    "garrafa termica",
    "panela de pressao",
    "jogo de panelas",
    "tapete de sala",
    "quadro decorativo",
    "tapete antiderrapante",
    # fitness e saude
    "tapete de yoga",
    "suplemento",
    "bico de papo",
    "multifuncional treino",
    "rolo de liberacao muscular",
    # moda e acessorios
    "tenis de corrida",
    "mochila",
    "oculos de sol",
    # beleza
    "secador de cabelo",
    "perfume",
    "kit skincare",
    # pet
    "comedouro para gato",
    "brinquedo para cachorro",
    "tapete higienico pet",
    "coleira antipulgas",
    "racao para cachorro",
    "bebedouro automatico gato",
    "areia para gato",
    # esporte
    "patinete eletrico",
)


def _ints(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = _str(name)
    if not raw:
        return default
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            continue
    return tuple(values) or default


@dataclass(frozen=True)
class AmazonSettings:
    access_key: str = ""
    secret_key: str = ""
    partner_id: str = ""
    partner_tag: str = ""
    region: str = "us-east-1"
    host: str = "webservices.amazon.com"
    keywords: tuple[str, ...] = ("notebook", "smartwatch", "fone bluetooth")

    @property
    def configured(self) -> bool:
        # O partner_id e opcional: o proprio tracking ID (partner_tag) ja
        # identifica a loja de afiliados na PA-API.
        return all([self.access_key, self.secret_key, self.partner_tag])


@dataclass(frozen=True)
class ShopeeSettings:
    partner_id: str = ""
    partner_key: str = ""
    shop_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    host: str = "partner.shopeebr.com"
    keywords: tuple[str, ...] = ("fone bluetooth", "smartwatch", "tenis")

    @property
    def configured(self) -> bool:
        # Endpoint de produto e shop-level: exige partner_id, partner_key,
        # shop_id e access_token.
        return all(
            [
                self.partner_id,
                self.partner_key,
                self.shop_id,
                self.access_token,
            ]
        )


@dataclass(frozen=True)
class MercadoLivreSettings:
    access_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    affiliate_id: str = ""
    site_id: str = "MLB"
    # Pagina da busca de produtos (a janela util da API vai ate 200 resultados).
    search_limit: int = 100
    search_offsets: tuple[int, ...] = (0, 100)
    # Quantos produtos vao ter os anuncios consultados (cada consulta custa 1
    # request e a API tem rate limit). Medido: 400 -> 4 ofertas aprovadas;
    # 700 -> 6 aprovadas em 7m42s; 900 -> as mesmas 6, mas 10 min. 700 e o
    # ponto ideal (mais sondagem nao rend mais nada).
    max_item_probes: int = 700
    max_items_per_product: int = 20
    request_delay_seconds: float = 0.3
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    # Sanidade da comparacao com o preco tipico: se o anuncio mais barato
    # custa menos de 35% da mediana, nao e desconto -- e outro produto
    # (condicao diferente, importado, etc). Nesse caso o desconto nao entra.
    comparison_min_ratio: float = 0.35
    # Termos de busca: o grupo precisa misturar nichos, nao ficar so em tech.
    keywords: tuple[str, ...] = DEFAULT_ML_KEYWORDS

    @property
    def configured(self) -> bool:
        # Precisa de access token pronto OU de client_id+client_secret
        # (nesse caso o proprio adapter obtem o token).
        return bool(self.access_token) or bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class Settings:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True

    database_url: str = "sqlite:///./data/offers.db"

    amazon: AmazonSettings = field(default_factory=AmazonSettings)
    shopee: ShopeeSettings = field(default_factory=ShopeeSettings)
    mercadolivre: MercadoLivreSettings = field(default_factory=MercadoLivreSettings)
    # Fontes ativas: amazon, shopee, mercadolivre
    sources: tuple[str, ...] = ("amazon", "shopee", "mercadolivre")

    min_discount_pct: float = 35.0
    # Desconto minimo quando a oferta vem do menor preco entre os vendedores
    # do mesmo produto (nao ha preco de tabela nesse caso).
    min_comparison_pct: float = 25.0
    min_price: float = 20.0
    max_price: float = 3000.0
    max_offers_per_cycle: int = 6
    # Quantas ofertas da mesma categoria (termo de busca) podem entrar no
    # mesmo ciclo. Com o teto em 1, o grupo nunca fica so com a categoria
    # que tem o maior desconto -- o mix e garantido.
    max_offers_per_keyword_per_cycle: int = 1
    resend_cooldown_hours: int = 24
    price_drop_retrigger_pct: float = 10.0
    # O padrao e NAO publicar oferta ficticia: quem quer modo demo liga
    # explicitamente (POST_DEMO_OFFERS=true). Um grupo real nunca deve
    # receber produto de teste sem pedir.
    post_demo_offers: bool = False

    interval_seconds: int = 7200
    send_delay_seconds: float = 1.2
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
            telegram_enabled=_bool("TELEGRAM_ENABLED", True),
            database_url=_str("DATABASE_URL", "sqlite:///./data/offers.db"),
            amazon=AmazonSettings(
                access_key=_str("AMAZON_ACCESS_KEY"),
                secret_key=_str("AMAZON_SECRET_KEY"),
                partner_id=_str("AMAZON_PARTNER_ID"),
                partner_tag=_str("AMAZON_PARTNER_TAG"),
                region=_str("AMAZON_REGION", "us-east-1"),
                host=_str("AMAZON_HOST", "webservices.amazon.com"),
                keywords=_keywords(
                    "KEYWORDS_AMAZON", ("notebook", "smartwatch", "fone bluetooth")
                ),
            ),
            shopee=ShopeeSettings(
                partner_id=_str("SHOPEE_PARTNER_ID"),
                partner_key=_str("SHOPEE_PARTNER_KEY"),
                shop_id=_str("SHOPEE_SHOP_ID"),
                access_token=_str("SHOPEE_ACCESS_TOKEN"),
                refresh_token=_str("SHOPEE_REFRESH_TOKEN"),
                host=_str("SHOPEE_HOST", "partner.shopeebr.com"),
                keywords=_keywords(
                    "KEYWORDS_SHOPEE", ("fone bluetooth", "smartwatch", "tenis")
                ),
            ),
            mercadolivre=MercadoLivreSettings(
                access_token=_str("MERCADOLIVRE_ACCESS_TOKEN"),
                client_id=_str("MERCADOLIVRE_CLIENT_ID"),
                client_secret=_str("MERCADOLIVRE_CLIENT_SECRET"),
                affiliate_id=_str("MERCADOLIVRE_AFFILIATE_ID"),
                site_id=_str("MERCADOLIVRE_SITE_ID", "MLB"),
                search_limit=_int("MERCADOLIVRE_SEARCH_LIMIT", 100),
                search_offsets=_ints(
                    "MERCADOLIVRE_SEARCH_OFFSETS", (0, 100)
                ),
                max_item_probes=_int("MERCADOLIVRE_MAX_PROBES", 400),
                max_items_per_product=_int("MERCADOLIVRE_MAX_ITEMS", 20),
                request_delay_seconds=_float("MERCADOLIVRE_REQUEST_DELAY", 0.3),
                max_retries=_int("MERCADOLIVRE_MAX_RETRIES", 3),
                retry_backoff_seconds=_float(
                    "MERCADOLIVRE_RETRY_BACKOFF", 2.0
                ),
                comparison_min_ratio=_float(
                    "MERCADOLIVRE_COMPARISON_MIN_RATIO", 0.35
                ),
                keywords=_keywords("KEYWORDS_MERCADOLIVRE", DEFAULT_ML_KEYWORDS),
            ),
            sources=_keywords("OFFERS_SOURCES", ("amazon", "shopee", "mercadolivre")),
            min_discount_pct=_float("MIN_DISCOUNT_PCT", 35.0),
            min_comparison_pct=_float("MIN_COMPARISON_PCT", 25.0),
            min_price=_float("MIN_PRICE", 20.0),
            max_price=_float("MAX_PRICE", 3000.0),
            max_offers_per_cycle=_int("MAX_OFFERS_PER_CYCLE", 6),
            max_offers_per_keyword_per_cycle=_int(
                "MAX_OFFERS_PER_KEYWORD_PER_CYCLE", 1
            ),
            resend_cooldown_hours=_int("RESEND_COOLDOWN_HOURS", 24),
            price_drop_retrigger_pct=_float("PRICE_DROP_RETRIGGER_PCT", 10.0),
            post_demo_offers=_bool("POST_DEMO_OFFERS", False),
            interval_seconds=_int("INTERVAL_SECONDS", 7200),
            send_delay_seconds=_float("SEND_DELAY_SECONDS", 1.2),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
        )

    def validate(self) -> list[str]:
        """Retorna a lista de problemas de configuracao (vazia = tudo certo)."""
        problems: list[str] = []
        if self.telegram_enabled and not self.telegram_token:
            problems.append("TELEGRAM_BOT_TOKEN nao configurado")
        if self.telegram_enabled and not self.telegram_chat_id:
            problems.append("TELEGRAM_CHAT_ID nao configurado")
        if not self.database_url:
            problems.append("DATABASE_URL nao configurado")
        elif _is_ci() and not self.database_url.startswith(("postgres://", "postgresql://")):
            # No runner do GitHub o filesystem e efemero: um SQLite local seria
            # recriado do zero a cada ciclo e a anti-duplicidade jamais
            # encontraria a oferta -- o bot repetiria as mesmas mensagens para
            # sempre. Falhar cedo e melhor do que duplicar no grupo.
            problems.append(
                "DATABASE_URL em CI precisa ser Postgres (Supabase). "
                "SQLite no runner e recriado a cada ciclo e faria o bot "
                "repetir as mesmas ofertas."
            )
        return problems
