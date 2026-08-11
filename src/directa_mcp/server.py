from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .client import (
    DirectaImportError,
    check_ports,
    historical_session,
    read_only_session,
    trading_session,
)
from .config import settings

mcp = FastMCP(
    "directa-mcp",
    instructions="""This server connects to Directa SIM's Darwin platform (dAPI) running
locally on this machine — it does not reach Directa over the internet.

Prerequisites the user must have in place: Darwin running and logged in with
API access enabled (Sviluppatori > Dev kit in Darwin, disclaimer signed on
directatrading.com). If any tool fails with a connection error, call
check_connection first and tell the user to verify Darwin is running.

Symbols follow Directa's own format, typically <TICKER>.MI for stocks listed
on Borsa Italiana (e.g. ENI.MI, ISP.MI).

Trading is gated: place_limit_order only sends a real order when the server
was started with DIRECTA_LIVE_TRADING=true in its environment; otherwise
every order runs in Darwin's simulation mode regardless of what is
requested. Always tell the user explicitly whether an order was live or
simulated using the `live` field in the tool's response.""",
)


def _import_error_result(exc: DirectaImportError) -> dict[str, Any]:
    return {"success": False, "error": str(exc)}


@mcp.tool()
def check_connection() -> dict[str, Any]:
    """Check whether Darwin's local trading and historical-data ports are
    reachable. Call this first if any other tool fails, or before assuming
    Darwin is ready — this does not require the account to be logged in,
    only Darwin to be running and listening."""
    return {"ports": check_ports()}


@mcp.tool()
def get_darwin_status() -> dict[str, Any]:
    """Get Darwin platform connection status and metrics — richer than
    check_connection since it goes through the trading API itself rather
    than a raw TCP probe. Prefer this once you know the ports are reachable."""
    try:
        with read_only_session() as api:
            return {"success": True, "data": api.get_darwin_status()}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_account_balance() -> dict[str, Any]:
    """Get account liquidity and balance information from Directa."""
    try:
        with read_only_session() as api:
            return {"success": True, "data": api.get_account_info()}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_positions() -> dict[str, Any]:
    """Get the current portfolio — open positions held in the Directa account."""
    try:
        with read_only_session() as api:
            return {"success": True, "data": api.get_portfolio()}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_orders() -> dict[str, Any]:
    """Get the status of orders placed today (pending, filled, cancelled)."""
    try:
        with read_only_session() as api:
            return {"success": True, "data": api.get_orders()}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def place_limit_order(
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: int,
    price: float,
) -> dict[str, Any]:
    """Place a limit order on Directa. Symbol format is <TICKER>.MI for Borsa
    Italiana stocks (e.g. ENI.MI). Runs in Darwin's simulation mode unless
    the server's DIRECTA_LIVE_TRADING environment variable is true — check
    the `live` field of the response to know which happened, and always
    surface that to the user before they assume an order was real."""
    try:
        with trading_session() as api:
            if side == "buy":
                result = api.buy_limit(symbol, quantity, price)
            else:
                result = api.sell_limit(symbol, quantity, price)
            return {"success": True, "live": settings.live_trading, "data": result}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def modify_order(order_id: str, price: float, signal_price: float | None = None) -> dict[str, Any]:
    """Modify the limit price of an existing open order. signal_price only
    applies to stop orders. Subject to the same live/simulation gating as
    place_limit_order — check the `live` field."""
    try:
        with trading_session() as api:
            result = api.modify_order(order_id, price, signal_price=signal_price)
            return {"success": True, "live": settings.live_trading, "data": result}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel a single open order by its ID (from get_orders)."""
    try:
        with trading_session() as api:
            return {
                "success": True,
                "live": settings.live_trading,
                "data": api.cancel_order(order_id),
            }
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def cancel_all_orders(symbol: str) -> dict[str, Any]:
    """Cancel every open order for a given symbol."""
    try:
        with trading_session() as api:
            return {
                "success": True,
                "live": settings.live_trading,
                "data": api.cancel_all_orders(symbol),
            }
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_daily_candles(symbol: str, days: int = 30) -> dict[str, Any]:
    """Get daily OHLC candles for a symbol over the last N days."""
    try:
        with historical_session() as api:
            return {"success": True, "data": api.get_daily_candles(symbol, days=days)}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_intraday_candles(
    symbol: str, days: int = 5, period_minutes: int = 5
) -> dict[str, Any]:
    """Get intraday OHLC candles for a symbol, bucketed by period_minutes,
    over the last N days."""
    try:
        with historical_session() as api:
            return {
                "success": True,
                "data": api.get_intraday_candles(
                    symbol, days=days, period_minutes=period_minutes
                ),
            }
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_tick_data(symbol: str, days: int = 1) -> dict[str, Any]:
    """Get tick-by-tick trade data for a symbol over the last N days. Can be
    large — prefer get_intraday_candles for anything beyond a single session."""
    try:
        with historical_session() as api:
            return {"success": True, "data": api.get_tick_data(symbol, days=days)}
    except DirectaImportError as exc:
        return _import_error_result(exc)


@mcp.tool()
def get_candle_data_range(
    symbol: str, start_date: str, end_date: str, period_seconds: int = 60
) -> dict[str, Any]:
    """Get OHLC candles for a symbol within an explicit date range.
    Dates as YYYYMMDDHHMMSS (e.g. 20260101093000 for 09:30:00 on 2026-01-01);
    period_seconds sets the candle bucket size (60=1min, 300=5min,
    3600=1hr, 86400=1day)."""
    try:
        with historical_session() as api:
            return {
                "success": True,
                "data": api.get_candle_data_range(
                    symbol, start_date, end_date, period_seconds=period_seconds
                ),
            }
    except DirectaImportError as exc:
        return _import_error_result(exc)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
