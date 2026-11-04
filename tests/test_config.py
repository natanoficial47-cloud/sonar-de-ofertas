"""Testes da configuracao lida do ambiente.

Regressao importante: `_keywords()` chegou a devolver `None` quando a variavel
de ambiente estava definida (o `return` do split tinha caido dentro de outra
funcao). No GitHub Actions o workflow sempre define `KEYWORDS_*` e
`OFFERS_SOURCES`, entao o bot quebraria com `TypeError: 'NoneType' is not
iterable` no primeiro job -- sem nenhuma mensagem de erro util.

Qualquer lista vinda do ambiente tem de ser uma tupla de strings, nunca None.
"""

from __future__ import annotations

import pytest

from config import Settings, _keywords


def test_keywords_de_variavel_de_ambiente(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEYWORDS_AMAZON", "notebook,smartwatch, fone bluetooth ")
    assert _keywords("KEYWORDS_AMAZON", ("padrao",)) == (
        "notebook",
        "smartwatch",
        "fone bluetooth",
    )


def test_keywords_ausente_usa_o_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KEYWORDS_AMAZON", raising=False)
    assert _keywords("KEYWORDS_AMAZON", ("padrao",)) == ("padrao",)


def test_keywords_vazio_usa_o_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEYWORDS_AMAZON", "")
    assert _keywords("KEYWORDS_AMAZON", ("padrao",)) == ("padrao",)


def test_keywords_com_virgulas_fora_ignora_itens_vazios(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("KEYWORDS_AMAZON", "notebook,,smartwatch,")
    assert _keywords("KEYWORDS_AMAZON", ("padrao",)) == ("notebook", "smartwatch")


def test_sources_do_ambiente_nunca_e_none(monkeypatch: pytest.MonkeyPatch):
    """`sources` alimenta o `build_adapters`: None quebraria o bot."""
    monkeypatch.setenv("OFFERS_SOURCES", "amazon,mercadolivre")
    settings = Settings.from_env()
    assert settings.sources == ("amazon", "mercadolivre")


def test_Keywords_de_cada_loja_nunca_e_none(monkeypatch: pytest.MonkeyPatch):
    """O padrao do workflow define as tres listas; nenhuma pode virar None."""
    monkeypatch.setenv("KEYWORDS_AMAZON", "notebook")
    monkeypatch.setenv("KEYWORDS_SHOPEE", "tenis")
    monkeypatch.setenv("KEYWORDS_MERCADOLIVRE", "air fryer,smartwatch")
    settings = Settings.from_env()
    assert settings.amazon.keywords == ("notebook",)
    assert settings.shopee.keywords == ("tenis",)
    assert settings.mercadolivre.keywords == ("air fryer", "smartwatch")


def test_nenhuma_lista_do_ambiente_fica_none(monkeypatch: pytest.MonkeyPatch):
    """Varredura ampla: nenhum campo de lista pode ser None com env populado."""
    monkeypatch.setenv("OFFERS_SOURCES", "mercadolivre")
    monkeypatch.setenv("KEYWORDS_AMAZON", "notebook")
    monkeypatch.setenv("KEYWORDS_SHOPEE", "tenis")
    monkeypatch.setenv("KEYWORDS_MERCADOLIVRE", "smartwatch")
    monkeypatch.setenv("MERCADOLIVRE_SEARCH_OFFSETS", "0,100")
    settings = Settings.from_env()
    for nome in ("amazon", "shopee", "mercadolivre"):
        lista = getattr(settings, nome).keywords
        assert lista is not None, f"keywords de {nome} ficou None"
        assert all(isinstance(p, str) and p for p in lista)
    assert settings.sources is not None
    assert settings.mercadolivre.search_offsets == (0, 100)
