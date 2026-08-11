"""Thin wrapper around the `directa-api-wrapper` package
(https://github.com/NiccoloSalvini/directa-api-python), verified against its
installed source under .venv/Lib/site-packages/directa_api:

- `from directa_api import DirectaTrading, HistoricalData` is the real
  import path (re-exported from the package's __init__.py).
- `DirectaTrading(host, port, buffer_size, simulation_mode, max_retries,
  retry_delay)` and `HistoricalData(host, port, buffer_size)` both support
  the context manager protocol — `__enter__` calls `.connect()` (a no-op in
  simulation mode), `__exit__` calls `.disconnect()`.
"""

import contextlib
import socket
from typing import Any, Iterator

from .config import settings


class DirectaImportError(RuntimeError):
    pass


def _import_directa_api() -> tuple[type, type]:
    try:
        from directa_api import DirectaTrading, HistoricalData
    except ImportError as exc:
        raise DirectaImportError(
            "Could not import directa_api. Install the project first: "
            "`uv pip install -e .` (or `pip install -e .`) from "
            "D:/dev/trading/directa-mcp — this pulls directa-api-wrapper "
            "from https://github.com/NiccoloSalvini/directa-api-python."
        ) from exc
    return DirectaTrading, HistoricalData


@contextlib.contextmanager
def read_only_session() -> Iterator[Any]:
    """Yield a DirectaTrading client connected for real, for read-only
    calls (balance, positions, orders, status). `simulation_mode` on this
    library gates the whole session, not individual calls — a simulated
    instance returns fake data even for get_portfolio()/get_account_info()
    — so reads always connect for real regardless of DIRECTA_LIVE_TRADING.
    This is safe: none of these calls mutate account state."""
    trading_cls, _ = _import_directa_api()
    with trading_cls(
        host=settings.trading_host,
        port=settings.trading_port,
        simulation_mode=False,
    ) as session:
        yield session


@contextlib.contextmanager
def trading_session() -> Iterator[Any]:
    """Yield a DirectaTrading client for order-mutating calls (place/modify/
    cancel). Runs in simulation mode unless DIRECTA_LIVE_TRADING=true is
    set — see .env.example."""
    trading_cls, _ = _import_directa_api()
    with trading_cls(
        host=settings.trading_host,
        port=settings.trading_port,
        simulation_mode=not settings.live_trading,
    ) as session:
        yield session


@contextlib.contextmanager
def historical_session() -> Iterator[Any]:
    """Yield a connected HistoricalData client."""
    _, historical_cls = _import_directa_api()
    with historical_cls(
        host=settings.trading_host,
        port=settings.historical_port,
    ) as session:
        yield session


def check_ports() -> dict[str, Any]:
    """Raw TCP connectivity check against Darwin's local ports — independent
    of whether the directa-api package imports correctly. Use this first to
    confirm Darwin is running and listening before debugging anything else."""
    results: dict[str, Any] = {}
    for label, port in (
        ("trading", settings.trading_port),
        ("historical", settings.historical_port),
    ):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            sock.connect((settings.trading_host, port))
            results[label] = {"host": settings.trading_host, "port": port, "reachable": True}
        except OSError as exc:
            results[label] = {
                "host": settings.trading_host,
                "port": port,
                "reachable": False,
                "error": str(exc),
            }
        finally:
            sock.close()
    return results
