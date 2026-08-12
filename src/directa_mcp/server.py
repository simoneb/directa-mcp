import functools
import time
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP

from .client import check_ports, historical_client, trading_client
from .config import settings
from .dapi import DapiConnectionError, DapiError, DapiTimeout

mcp = FastMCP(
    "directa-mcp",
    instructions="""This server connects to Directa SIM's Darwin platform (dAPI) running
locally on this machine — it does not reach Directa over the internet.

Prerequisites the user must have in place: Darwin running and logged in with
API access enabled (Sviluppatori > Dev kit in Darwin, disclaimer signed on
directatrading.com). If any tool fails with a connection error, call
check_connection first and tell the user to verify Darwin is running.

Symbols follow Directa's own format: <TICKER>.MI for stocks on Borsa Italiana
(e.g. ENI.MI), bare tickers for ETFs (e.g. VWCE), and M.<number> for bonds.
Read a symbol back from get_positions rather than guessing at its format.

Historical data (candles, ticks) requires the real-time quote entitlement on
the account. Without it every historical tool fails with dAPI code 1032,
"datafeed non abilitato" — report that to the user as an account setting to
enable, not as a bug or a transient error.

Order tools are disabled unless the server was started with
DIRECTA_ENABLE_ORDERS=true. When disabled they send nothing at all; there is no
simulation fallback, so a disabled response never resembles a placed order.
The order tools are also the one part of this server not verified against live
Darwin — treat their first real use as a test, and confirm the outcome with
get_orders rather than trusting the acknowledgement alone.""",
)

# Errors that mean "the request failed" rather than "the code is broken".
_DAPI_ERRORS = (DapiError, DapiConnectionError, DapiTimeout)


class OrdersDisabled(RuntimeError):
    """An order tool was called while the order gate is closed."""


def _require_orders_enabled() -> None:
    if not settings.orders_enabled:
        raise OrdersDisabled(
            "Order tools are disabled because the server was not started with "
            "DIRECTA_ENABLE_ORDERS=true. Nothing was sent to Darwin — no order was "
            "placed, modified or cancelled."
        )


def tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Register an MCP tool whose `success` flag reflects what actually
    happened. Wrapping a failure as a successful call with the error buried in
    the payload — as the previous version did — makes a caller believe a reply
    it should not trust. A blocked order counts as a failure here for the same
    reason: it must not read like an order that went through."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"success": True, **fn(*args, **kwargs)}
        except OrdersDisabled as exc:
            return {"success": False, "blocked": True, "sent_to_darwin": False, "error": str(exc)}
        except DapiError as exc:
            return {
                "success": False,
                "error": str(exc),
                "error_code": exc.code,
                "command": exc.command,
            }
        except _DAPI_ERRORS as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    return mcp.tool()(wrapper)


@tool
def check_connection() -> dict[str, Any]:
    """Check whether Darwin's local trading and historical-data ports are
    reachable. Call this first if any other tool fails: it distinguishes
    "Darwin is not running" from "Darwin refused the command", and needs only
    Darwin running and listening, not the account logged in."""
    return {"ports": check_ports(), "orders_enabled": settings.orders_enabled}


@tool
def get_darwin_status() -> dict[str, Any]:
    """Get Darwin's connection status, release, and whether the real-time
    datafeed is enabled. Richer than check_connection since it goes through the
    dAPI conversation rather than a raw TCP probe. The `datafeed_enabled` field
    tells you in advance whether the historical tools can work at all."""
    with trading_client() as api:
        return {"data": api.darwin_status()}


@tool
def get_account_balance() -> dict[str, Any]:
    """Get raw account figures from dAPI INFOACCOUNT: liquidity, equity and two
    fields whose documented names do not match their contents.

    `gain_euro` is the open P&L, and `open_profit_loss` is the portfolio's cost
    basis, not a profit — both verified by reconciliation. Do not quote
    `open_profit_loss` as a gain. For anything about portfolio value or
    performance prefer get_portfolio_overview, which derives the figures and
    checks them; use this tool for liquidity, equity, the account code, or when
    the user wants the raw line."""
    with trading_client() as api:
        return {"data": api.account_info()}


@tool
def get_availability() -> dict[str, Any]:
    """Get buying power: cash available for stocks and derivatives, with and
    without margin (dAPI INFOAVAILABILITY). Use this rather than
    get_account_balance when the question is "how much can I invest"."""
    with trading_client() as api:
        return {"data": api.availability()}


@tool
def get_positions() -> dict[str, Any]:
    """Get every open position in the portfolio, with quantity, average price
    and theoretical gain. Returns the complete list."""
    with trading_client() as api:
        positions = api.positions()
        return {"count": len(positions), "data": positions}


@tool
def get_portfolio_overview() -> dict[str, Any]:
    """The whole picture in one call: every position with its current price and
    value, plus portfolio totals and P&L. Prefer this over get_positions when
    the question is about how the portfolio is doing or what it is worth.

    Darwin does not report a current price, so price and value are derived from
    the average price and the theoretical gain. Do not compute values yourself
    from get_positions: bonds (symbols starting `M.`) are quoted as a
    percentage of nominal, so quantity times price overstates them 100-fold.
    This tool handles that and then checks its own arithmetic against Darwin's
    reported figures — if `reconciliation.reconciled` is False, say so and quote
    the residual rather than presenting the totals as fact."""
    with trading_client() as api:
        return {"data": api.portfolio_overview()}


@tool
def get_position(symbol: str) -> dict[str, Any]:
    """Get a single position by symbol, as it appears in get_positions."""
    with trading_client() as api:
        return {"data": api.position(symbol)}


@tool
def get_orders(pending_only: bool = False, symbol: str | None = None) -> dict[str, Any]:
    """Get orders placed today with their state — "In negoziazione" (working),
    "Eseguito" (filled), "Revocato" (cancelled), and so on.

    A symbol usually carries several order records with different states, so
    read the `status` field before describing an order as open; set
    pending_only=True for just the ones still working."""
    with trading_client() as api:
        orders = api.orders(pending_only=pending_only, symbol=symbol)
        return {"count": len(orders), "data": orders}


@tool
def place_limit_order(
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: int,
    price: float,
    order_id: str | None = None,
) -> dict[str, Any]:
    """Place a limit order. Disabled unless the server runs with
    DIRECTA_ENABLE_ORDERS=true, in which case this places a REAL order with real
    money — there is no simulation mode.

    order_id is the client-side reference Darwin echoes back; one is generated
    if omitted. If the response has confirmation_required, the order is NOT on
    the market until confirm_order is called with the same id. Always verify
    with get_orders afterwards."""
    _require_orders_enabled()
    with trading_client() as api:
        reference = order_id or f"MCP{int(time.time())}"
        return {"sent_to_darwin": True, "data": api.place_limit_order(symbol, side, quantity, price, reference)}


@tool
def modify_order(
    order_id: str, price: float, signal_price: float | None = None
) -> dict[str, Any]:
    """Change the limit price of an open order (signal_price applies only to
    stop orders). Same order gate as place_limit_order."""
    _require_orders_enabled()
    with trading_client() as api:
        return {"sent_to_darwin": True, "data": api.modify_order(order_id, price, signal_price)}


@tool
def confirm_order(order_id: str) -> dict[str, Any]:
    """Confirm an order that came back with confirmation_required. Until this
    succeeds the order is not on the market. Same order gate."""
    _require_orders_enabled()
    with trading_client() as api:
        return {"sent_to_darwin": True, "data": api.confirm_order(order_id)}


@tool
def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel one open order by the order_id shown in get_orders. Same
    order gate as place_limit_order."""
    _require_orders_enabled()
    with trading_client() as api:
        return {"sent_to_darwin": True, "data": api.cancel_order(order_id)}


@tool
def cancel_all_orders(symbol: str) -> dict[str, Any]:
    """Cancel every open order for a symbol. Confirm the symbol with the user
    first — this affects orders the user may not have asked about. Same
    order gate as place_limit_order."""
    _require_orders_enabled()
    with trading_client() as api:
        return {"sent_to_darwin": True, "data": api.cancel_all_orders(symbol)}


@tool
def get_daily_candles(symbol: str, days: int = 30) -> dict[str, Any]:
    """Get daily OHLC candles for the last N days. Needs the real-time quote
    entitlement — without it this fails with code 1032."""
    with historical_client() as api:
        candles = api.candles(symbol, days, 86400)
        return {"count": len(candles), "data": candles}


@tool
def get_intraday_candles(
    symbol: str, days: int = 5, period_minutes: int = 5
) -> dict[str, Any]:
    """Get intraday OHLC candles bucketed by period_minutes over the last N
    days. Needs the real-time quote entitlement (code 1032 without it)."""
    with historical_client() as api:
        candles = api.candles(symbol, days, period_minutes * 60)
        return {"count": len(candles), "data": candles}


@tool
def get_candle_data_range(
    symbol: str, start_date: str, end_date: str, period_seconds: int = 60
) -> dict[str, Any]:
    """Get OHLC candles within an explicit range. Dates as YYYYMMDDHHMMSS
    (e.g. 20260810093000); period_seconds sets the bucket (60=1min, 300=5min,
    3600=1hr, 86400=1day). Needs the real-time quote entitlement."""
    with historical_client() as api:
        candles = api.candles_range(symbol, start_date, end_date, period_seconds)
        return {"count": len(candles), "data": candles}


@tool
def get_tick_data(symbol: str, days: int = 1) -> dict[str, Any]:
    """Get tick-by-tick trades over the last N days. Can be very large — prefer
    get_intraday_candles beyond a single session. Needs the real-time quote
    entitlement."""
    with historical_client() as api:
        ticks = api.ticks(symbol, days)
        return {"count": len(ticks), "data": ticks}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
