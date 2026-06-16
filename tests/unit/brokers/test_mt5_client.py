from types import SimpleNamespace
from unittest.mock import patch

from src.brokers.mt5.client import Mt5Client
from src.config.settings import Mt5Config


def _client(**mappings: str) -> Mt5Client:
    return Mt5Client(
        Mt5Config(
            login=1,
            password="",
            server="test",
            path="",
            symbol_mappings=mappings,
        )
    )


def test_resolve_symbol_requires_config_mapping_for_gold_exness_alias() -> None:
    client = _client()

    with (
        patch(
            "src.brokers.mt5.client.mt5.symbols_get",
            return_value=[SimpleNamespace(name="XAUUSDm")],
        ),
        patch("src.brokers.mt5.client.mt5.symbol_select") as select,
    ):
        assert client.resolve_symbol("XAU/USD") is None

    select.assert_not_called()


def test_resolve_symbol_uses_configured_us100_exness_alias() -> None:
    client = _client(US100="USTEC_x100m")

    with (
        patch(
            "src.brokers.mt5.client.mt5.symbols_get",
            return_value=[SimpleNamespace(name="USTEC_x100m")],
        ),
        patch("src.brokers.mt5.client.mt5.symbol_select", return_value=True) as select,
    ):
        assert client.resolve_symbol("US100") == "USTEC_x100m"

    select.assert_called_once_with("USTEC_x100m", True)


def test_resolve_symbol_requires_config_mapping_for_us100_exness_alias() -> None:
    client = _client()

    with (
        patch(
            "src.brokers.mt5.client.mt5.symbols_get",
            return_value=[SimpleNamespace(name="USTEC_x100m")],
        ),
        patch("src.brokers.mt5.client.mt5.symbol_select") as select,
    ):
        assert client.resolve_symbol("US100") is None

    select.assert_not_called()


def test_resolve_symbol_uses_explicit_mapping_exactly() -> None:
    client = _client(XAUUSD="XAUUSDm")

    with (
        patch(
            "src.brokers.mt5.client.mt5.symbols_get",
            return_value=[
                SimpleNamespace(name="XAUUSD.pro"),
                SimpleNamespace(name="XAUUSDm"),
            ],
        ),
        patch("src.brokers.mt5.client.mt5.symbol_select", return_value=True) as select,
    ):
        assert client.resolve_symbol("XAU_USD") == "XAUUSDm"

    select.assert_called_once_with("XAUUSDm", True)


def test_resolve_symbol_prefers_configured_mapping_over_canonical_exact() -> None:
    client = _client(US100="USTEC_x100m")

    with (
        patch(
            "src.brokers.mt5.client.mt5.symbols_get",
            return_value=[
                SimpleNamespace(name="USTEC_x100m"),
                SimpleNamespace(name="US100"),
            ],
        ),
        patch("src.brokers.mt5.client.mt5.symbol_select", return_value=True) as select,
    ):
        assert client.resolve_symbol("US100") == "USTEC_x100m"

    select.assert_called_once_with("USTEC_x100m", True)


def test_resolve_symbol_accepts_exact_canonical_symbol() -> None:
    client = _client()

    with (
        patch(
            "src.brokers.mt5.client.mt5.symbols_get",
            return_value=[SimpleNamespace(name="EURUSD")],
        ),
        patch("src.brokers.mt5.client.mt5.symbol_select", return_value=True),
    ):
        assert client.resolve_symbol("EUR/USD") == "EURUSD"
