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
        return all(
            [
                self.access_key,
                self.secret_key,
                self.partner_id,
                self.partner_tag,
            ]
        )


@dataclass(frozen=True)
class ShopeeSettings:
    partner_id: str = ""
    partner_key: str = ""
    shop_id: str = ""
    host: str = "partner.shopeebr.com"
    keywords: tuple[str, ...] = ("fone bluetooth", "smartwatch", "tenis")

    @property
    def configured(self) -> bool:
        return all([self.partner_id, self.partner_key])


@dataclass(frozen=True)
class MercadoLivreSettings:
    access_token: str = ""
    affiliate_id: str = ""
    site_id: str = "MLB"
    keywords: tuple[str, ...] = ("fone bluetooth", "smartwatch", "cadeira gamer")

    @property
    def configured(self) -> bool:
        # A API do Mercado Livre exige token (403 sem ele).
        return bool(self.access_token)


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
    sources: tuple[str, ...] = ("mercadolivre",)

    min_discount_pct: float = 35.0
    min_price: float = 49.9
    max_price: float = 3000.0
    max_offers_per_cycle: int = 5
    resend_cooldown_hours: int = 24
    price_drop_retrigger_pct: float = 10.0
    post_demo_offers: bool = True

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
                host=_str("SHOPEE_HOST", "partner.shopeebr.com"),
                keywords=_keywords(
                    "KEYWORDS_SHOPEE", ("fone bluetooth", "smartwatch", "tenis")
                ),
            ),
            mercadolivre=MercadoLivreSettings(
                access_token=_str("MERCADOLIVRE_ACCESS_TOKEN"),
                affiliate_id=_str("MERCADOLIVRE_AFFILIATE_ID"),
                site_id=_str("MERCADOLIVRE_SITE_ID", "MLB"),
                keywords=_keywords(
                    "KEYWORDS_MERCADOLIVRE",
                    ("fone bluetooth", "smartwatch", "cadeira gamer"),
                ),
            ),
            sources=_keywords("OFFERS_SOURCES", ("mercadolivre",)),
            min_discount_pct=_float("MIN_DISCOUNT_PCT", 35.0),
            min_price=_float("MIN_PRICE", 49.9),
            max_price=_float("MAX_PRICE", 3000.0),
            max_offers_per_cycle=_int("MAX_OFFERS_PER_CYCLE", 5),
            resend_cooldown_hours=_int("RESEND_COOLDOWN_HOURS", 24),
            price_drop_retrigger_pct=_float("PRICE_DROP_RETRIGGER_PCT", 10.0),
            post_demo_offers=_bool("POST_DEMO_OFFERS", True),
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
        return problems
