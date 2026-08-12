"""Session helpers binding the dAPI client to this server's configuration.

Both helpers are context managers that open a fresh connection per call.
Darwin pushes a full portfolio and order snapshot on every connect, so a
short-lived connection is cheap and avoids holding a socket open between
tool calls.
"""

from typing import Any

from . import dapi
from .config import settings


def trading_client() -> dapi.TradingClient:
    """A trading-port client, configured from the environment. Use as a
    context manager."""
    return dapi.TradingClient(host=settings.trading_host, port=settings.trading_port)


def historical_client() -> dapi.HistoricalClient:
    """A historical-data-port client, configured from the environment. Use as a
    context manager."""
    return dapi.HistoricalClient(host=settings.trading_host, port=settings.historical_port)


def check_ports() -> dict[str, Any]:
    """Raw TCP reachability of Darwin's ports — see dapi.check_ports."""
    return dapi.check_ports(
        settings.trading_host,
        (("trading", settings.trading_port), ("historical", settings.historical_port)),
    )
