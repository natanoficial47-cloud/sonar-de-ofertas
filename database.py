"""Persistencia das ofertas ja enviadas.

O mesmo codigo roda sobre SQLite (dev local e testes) e PostgreSQL (producao
no GitHub Actions via Supabase). A diferenca fica isolada em `_sql`, que troca
o placeholder de parametros, e no tipo da conexao.

Anti-duplicidade em tres niveis:
  1. hash da URL -> nunca reenvia a MESMA oferta identica;
  2. cooldown    -> so reenvia o mesmo produto depois de N horas;
  3. re-trigger  -> reenvia antes do cooldown se o preco caiu o suficiente.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from models import Offer

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    url_hash        TEXT PRIMARY KEY,
    store           TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    current_price   REAL NOT NULL,
    original_price  REAL,
    discount_pct    REAL NOT NULL DEFAULT 0,
    is_demo         INTEGER NOT NULL DEFAULT 0,
    first_seen_at   BIGINT NOT NULL,
    last_sent_at    BIGINT,
    send_count      INTEGER NOT NULL DEFAULT 0,
    product_key     TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   BIGINT NOT NULL,
    finished_at  BIGINT,
    status       TEXT NOT NULL,
    offers_found INTEGER NOT NULL DEFAULT 0,
    offers_sent  INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
"""


class Database:
    def __init__(self, url: str):
        self.url = url
        self.backend = "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._conn: Any = None

    # ------------------------------------------------------------------ setup
    def connect(self) -> "Database":
        if self.backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(self.url, row_factory=dict_row)
        else:
            path = self._sqlite_path()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()
        return self

    def _sqlite_path(self) -> str:
        raw = self.url
        for prefix in ("sqlite:///", "sqlite://"):
            if raw.startswith(prefix):
                return raw[len(prefix) :]
        return raw

    def _sql(self, sql: str) -> str:
        """Normaliza placeholders para o driver do backend."""
        if self.backend == "postgres":
            return sql.replace("?", "%s")
        return sql

    def migrate(self) -> None:
        if self.backend == "postgres":
            # No Postgres AUTOINCREMENT nao existe: usa SERIAL/IDENTITY.
            schema = SCHEMA.replace(
                "id           INTEGER PRIMARY KEY AUTOINCREMENT,",
                "id           BIGSERIAL PRIMARY KEY,",
            )
        else:
            schema = SCHEMA

        for statement in [s.strip() for s in schema.split(";") if s.strip()]:
            with self._cursor() as cur:
                cur.execute(self._sql(statement))
        self._conn.commit()

        # Bancos criados antes da coluna product_key existem no Supabase do
        # usuario: o ALTER e idempotente (ignorar "ja existe"). Precisa
        # acontecer ANTES do indice, senao o indice quebra em tabela velha.
        try:
            with self._cursor() as cur:
                cur.execute(self._sql("ALTER TABLE offers ADD COLUMN product_key TEXT"))
            self._conn.commit()
        except Exception:
            self._conn.rollback()  # a coluna ja existe: nada a fazer

        try:
            with self._cursor() as cur:
                cur.execute(
                    self._sql(
                        "CREATE INDEX IF NOT EXISTS idx_offers_product_key "
                        "ON offers (product_key)"
                    )
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        # O cursor do sqlite3 nao implementa o protocolo de context manager,
        # por isso o fechamento e explicito.
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # ------------------------------------------------------------- anti-dup
    def get_offer(self, url_hash: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(self._sql("SELECT * FROM offers WHERE url_hash = ?"), (url_hash,))
            row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def record_seen(self, offer: Offer, now: int | None = None) -> None:
        """Registra que a oferta foi vista (sem necessariamente ter sido enviada)."""
        now = now or int(time.time())
        with self._cursor() as cur:
            cur.execute(
                self._sql(
                    """
                    INSERT INTO offers (
                        url_hash, store, external_id, title, url, current_price,
                        original_price, discount_pct, is_demo, first_seen_at,
                        last_sent_at, send_count, product_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                    ON CONFLICT (url_hash) DO UPDATE SET
                        current_price  = EXCLUDED.current_price,
                        original_price = EXCLUDED.original_price,
                        discount_pct   = EXCLUDED.discount_pct,
                        title          = EXCLUDED.title,
                        product_key    = COALESCE(EXCLUDED.product_key, offers.product_key)
                    """
                ),
                (
                    offer.url_hash,
                    offer.store,
                    offer.external_id,
                    offer.title,
                    offer.url,
                    float(offer.current_price),
                    float(offer.original_price) if offer.original_price else None,
                    offer.effective_discount,
                    1 if offer.is_demo else 0,
                    now,
                    offer.product_key,
                ),
            )
        self._conn.commit()

    def mark_sent(self, offer: Offer, now: int | None = None) -> None:
        now = now or int(time.time())
        with self._cursor() as cur:
            cur.execute(
                self._sql(
                    """
                    UPDATE offers
                    SET last_sent_at = ?,
                        send_count   = send_count + 1,
                        current_price = ?,
                        discount_pct  = ?
                    WHERE url_hash = ?
                    """
                ),
                (
                    now,
                    float(offer.current_price),
                    offer.effective_discount,
                    offer.url_hash,
                ),
            )
        self._conn.commit()

    def _latest_for_product(self, offer: Offer) -> dict | None:
        """Registro mais recente do PRODUTO (qualquer anuncio do mesmo produto).

        Sem isso, o Mercado Livre reentrava no grupo com o mesmo produto: o
        bot escolhe o anuncio mais barato entre os vendedores e, quando o
        preco oscila entre eles, o `item_id` (e a URL) mudam -- o hash da
        URL dizia "oferta nova" e o produto voltava ao grupo.
        """
        key = offer.product_key
        if not key:
            return self.get_offer(offer.url_hash)
        with self._cursor() as cur:
            # `last_sent_at IS NULL` primeiro: quem ja foi enviado tem
            # prioridade sobre o registro que so foi visto.
            cur.execute(
                self._sql(
                    """
                    SELECT * FROM offers
                    WHERE product_key = ?
                    ORDER BY (last_sent_at IS NULL), last_sent_at DESC, first_seen_at DESC
                    LIMIT 1
                    """
                ),
                (key,),
            )
            row = cur.fetchone()
        if row is None:
            # Registros antigos (antes da coluna product_key) so tem a URL.
            return self.get_offer(offer.url_hash)
        return dict(row)

    def should_send(
        self,
        offer: Offer,
        now: int | None = None,
        cooldown_hours: int = 24,
        price_drop_retrigger_pct: float = 10.0,
    ) -> tuple[bool, str]:
        """Decide se a oferta pode ser enviada agora. Retorna (enviar, motivo)."""
        now = now or int(time.time())
        row = self._latest_for_product(offer)

        if row is None:
            return True, "primeira vez"
        if not row.get("last_sent_at"):
            return True, "ja vista mas nunca enviada"

        last_sent_at = int(row["last_sent_at"])
        elapsed_hours = (now - last_sent_at) / 3600.0

        # Queda de preco so vale no MESMO anuncio. Comparar com o preco de
        # outro vendedor do mesmo produto faria o bot reenviar o produto
        # sempre que o preco oscila entre os anuncios -- de novo, a
        # duplicata que a gente quer evitar.
        same_listing = self.get_offer(offer.url_hash)
        if same_listing and same_listing.get("last_sent_at"):
            previous_price = float(same_listing["current_price"] or 0)
            if previous_price > 0 and offer.current_price < previous_price:
                drop_pct = (1 - offer.current_price / previous_price) * 100
                if drop_pct >= price_drop_retrigger_pct:
                    return True, f"preco caiu {drop_pct:.0f}% (de {previous_price:.2f})"

        if elapsed_hours >= cooldown_hours:
            return True, f"cooldown de {cooldown_hours}h expirado ({elapsed_hours:.1f}h)"

        return False, f"duplicada (produto ja enviado ha {elapsed_hours:.1f}h)"

    # ---------------------------------------------------------------- runs
    def start_run(self, now: int | None = None) -> int:
        now = now or int(time.time())
        with self._cursor() as cur:
            cur.execute(
                self._sql(
                    "INSERT INTO runs (started_at, status, offers_found, offers_sent) "
                    "VALUES (?, ?, 0, 0) RETURNING id"
                ),
                (now, "running"),
            )
            row = cur.fetchone()
            run_id = int(dict(row)["id"])
        self._conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        offers_found: int = 0,
        offers_sent: int = 0,
        error: str | None = None,
        now: int | None = None,
    ) -> None:
        now = now or int(time.time())
        with self._cursor() as cur:
            cur.execute(
                self._sql(
                    "UPDATE runs SET finished_at = ?, status = ?, offers_found = ?, "
                    "offers_sent = ?, error = ? WHERE id = ?"
                ),
                (now, status, offers_found, offers_sent, error, run_id),
            )
        self._conn.commit()

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                self._sql("SELECT * FROM runs ORDER BY id DESC LIMIT ?"),
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- kv
    def kv_get(self, key: str) -> str | None:
        with self._cursor() as cur:
            cur.execute(self._sql("SELECT value FROM kv WHERE key = ?"), (key,))
            row = cur.fetchone()
        if row is None:
            return None
        value = row["value"] if isinstance(row, dict) else row[0]
        return str(value) if value else None

    def kv_set(self, key: str, value: str, now: int | None = None) -> None:
        """Guarda um valor (ex.: access_token renovado da Shopee)."""
        now = now or int(time.time())
        with self._cursor() as cur:
            cur.execute(
                self._sql(
                    """
                    INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                (key, value, now),
            )
        self._conn.commit()
