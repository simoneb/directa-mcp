"""Client for Directa SIM's Darwin API (dAPI) — the line-oriented TCP protocol
Darwin exposes on localhost.

Every command and response format used here was verified against a live Darwin
2.5.1; see docs/PROTOCOL.md for the raw transcripts. This module replaces the
third-party `directa-api-wrapper`, which on that build:

  * sent GETPORTFOLIO and GETACCTINFO, both of which Darwin rejects with
    ERR 1004 — the commands Darwin accepts are INFOSTOCKS and INFOACCOUNT;
  * returned only the first matching line of a multi-line response, silently
    reducing a 15-position portfolio, or a 4-order list, to a single row.

Framing here is deterministic rather than time-based: `FLOWPOINT TRUE` makes
Darwin wrap list responses in BEGIN/END markers, so a list is read to its
terminator instead of until the socket happens to fall quiet. Darwin also
pushes unsolicited updates (a full portfolio and order list on connect, then
position and order changes as they happen), so reads skip lines that do not
belong to the command in flight rather than mistaking them for the response.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# dAPI error codes. Reproduced from Directa's documentation; 1004 and 1032 are
# the two we have observed in practice.
ERROR_CODES: dict[str, str] = {
    "1000": "ERR_MAX_SUBSCRIPTION_OVERFLOW - Limite massimo di titoli sottoscritti raggiunto",
    "1001": "ERR_ALREADY_SUBSCRIBED - Titolo richiesto già sottoscritto",
    "1002": "ERR_EMPTY_LIST - Nessun titolo inviato nel comando",
    "1003": "ERR_UNKNOWN_COMMAND - Comando sconosciuto",
    "1004": "ERR_COMMAND_NOT_EXECUTED - Comando non eseguito",
    "1005": "ERR_NOT_SUBSCRIBED - Errore sottoscrizione",
    "1006": "ERR_DARWIN_STOP - Chiusura Darwin in corso",
    "1007": "ERR_BAD_SUBSCRIPTION - Errore titolo inesistente",
    "1008": "ERR_DATA_UNAVAILABLE - Flusso richiesto non disponibile",
    "1009": "ERR_TRADING_CMD_INCOMPLETE - Comando trading non completo",
    "1010": "ERR_TRADING_CMD_ERROR - Comando trading errato",
    "1011": "ERR_TRADING_UNAVAILABLE - Trading non abilitato",
    "1012": "ERR_TRADING_REQUEST_ERROR - Errore immissione ordine",
    "1013": "ERR_HISTORYCALL_PARAMS - Errore numero parametri nel comando",
    "1015": "ERR_HISTORYCALL_RANGE_INTRADAY - Errore range per chiamate intraday",
    "1016": "ERR_HISTORYCALL_DAY_OR_RANGE - Errore nei giorni o nel range date",
    "1018": "ERR_EMPTY_STOCKLIST - Nessuno strumento nel portafoglio",
    "1019": "ERR_EMPTY_ORDERLIST - Nessun ordine presente",
    "1020": "ERR_DUPLICATED_ID - ID Ordine duplicato",
    "1021": "ERR_INVALID_ORDER_STATE - Stato ordine incongruente con l'operazione richiesta",
    "1024": "ERR_TRADING_PUSH_DISCONNECTED - Segnala la disconnessione del trading",
    "1025": "ERR_TRADING_PUSH_RECONNECTION_OK - Segnale di riconnessione",
    "1026": "ERR_TRADING_PUSH_RELOAD - Segnala il reload del trading",
    "1027": "ERR_DATAFEED_DISCONNECTED - Segnala la disconnessione del datafeed",
    "1028": "ERR_DATAFEED_RELOAD - Segnala il reload del datafeed",
    "1030": "ERR_MARKET_UNAVAILABLE - Mercato non abilitato per il ticker richiesto",
    "1031": "CONTATTO_NON_ATTIVO - Contatto verso il server di trading scaduto, riavviare l'applicazione",
    "1032": "DATAFEED NON ABILITATO - Quotazioni non abilitate",
}

# Symbols on Directa's bond market carry this prefix and are quoted as a
# percentage of nominal value: 20000 nominal at 95.00 is 19,000 euro, not
# 1,900,000. Multiplying quantity by price without accounting for this
# overstates a bond position by 100x. Verified by reconciliation against a live
# account: applying the convention to every bond held matched Darwin''s own
# cost-basis figure to the cent, and the derived prices landed inside the range
# of that account''s working orders, where the naive figures did not.
PERCENT_QUOTED_PREFIX = "M."

# Order states as reported in the last field of an ORDER line.
ORDER_STATUS: dict[str, str] = {
    "2000": "In negoziazione",
    "2001": "Errore immissione",
    "2002": "In negoziazione dopo conferma ricevuta",
    "2003": "Eseguito",
    "2004": "Revocato",
    "2005": "In attesa di conferma",
    "2006": "Modificato",
}

# ERR lines carrying these codes are asynchronous notifications about the
# trading/datafeed link, not a refusal of the command in flight. Treating them
# as command failures would surface a spurious error on an unrelated call.
NOTIFICATION_CODES = frozenset({"1024", "1025", "1026", "1027", "1028"})

# Codes that mean "the list you asked for is empty" — an empty result, not an
# error. An account with no positions must not look like a broken connection.
EMPTY_LIST_CODES = frozenset({"1018", "1019"})


class DapiError(RuntimeError):
    """Darwin refused a command, answering ERR;<subject>;<code>."""

    def __init__(self, command: str, code: str, subject: str = "") -> None:
        self.command = command
        self.code = code
        self.subject = subject
        self.description = ERROR_CODES.get(code, "Unknown dAPI error code")
        super().__init__(f"{command} failed: ERR {code} — {self.description}")


class DapiConnectionError(RuntimeError):
    """Could not reach Darwin, or it closed the connection mid-exchange."""


class DapiTimeout(RuntimeError):
    """Darwin accepted the command but never sent a complete response."""


def _decode_error(line: str) -> tuple[str, str]:
    """Split an `ERR;<subject>;<code>` line. The subject is the command name on
    the trading port but the account code on the historical port, so it is
    reported rather than relied upon."""
    parts = line.split(";")
    subject = parts[1] if len(parts) > 1 else ""
    code = parts[2].strip() if len(parts) > 2 else ""
    return subject, code


def _sum_or_none(*values: Any) -> float | None:
    """Sum, or None if any term is not a number — so a missing field yields no
    figure rather than a figure computed from a guess."""
    if not all(isinstance(v, (int, float)) for v in values):
        return None
    return round(sum(values), 2)


def _difference_or_none(left: Any, right: Any) -> float | None:
    if not all(isinstance(v, (int, float)) for v in (left, right)):
        return None
    return round(left - right, 2)


def _number(raw: str) -> Any:
    """Convert a dAPI numeric field, leaving anything unparseable as a string
    so a format surprise shows up in the output instead of raising."""
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


@dataclass
class _Connection:
    """Line-framed socket conversation with one Darwin port."""

    host: str
    port: int
    service: str
    connect_timeout: float = 5.0
    buffer_size: int = 8192

    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _buffer: bytes = field(default=b"", init=False, repr=False)
    # Unsolicited lines seen while waiting for a response, kept for callers
    # that want them (the connect-time portfolio push is genuine data).
    unsolicited: list[str] = field(default_factory=list, init=False, repr=False)

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            raise DapiConnectionError(
                f"Cannot reach Darwin's {self.service} port at {self.host}:{self.port} "
                f"({exc}). Check that Darwin is running and logged in with API access "
                f"enabled (Sviluppatori > Dev kit)."
            ) from exc
        self._buffer = b""
        self.unsolicited = []

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def drain(self, idle: float = 1.0) -> list[str]:
        """Collect whatever Darwin has already pushed, until `idle` seconds pass
        with nothing new. Used for the connect-time push, which arrives without
        any command having been sent."""
        lines: list[str] = []
        while True:
            line = self._next_line(time.monotonic() + idle)
            if line is None:
                return lines
            if line:
                lines.append(line)

    def send(self, command: str) -> None:
        if self._sock is None:
            raise DapiConnectionError(f"Not connected to Darwin's {self.service} port")
        self._sock.sendall((command + "\r\n").encode("latin-1"))

    def _next_line(self, deadline: float) -> str | None:
        """Next complete line, or None once `deadline` passes."""
        if self._sock is None:
            raise DapiConnectionError(f"Not connected to Darwin's {self.service} port")
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
                return raw.decode("latin-1").strip()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(min(0.5, remaining))
            try:
                chunk = self._sock.recv(self.buffer_size)
            except socket.timeout:
                continue
            except OSError as exc:
                raise DapiConnectionError(
                    f"Darwin's {self.service} connection failed mid-read: {exc}"
                ) from exc
            if not chunk:
                raise DapiConnectionError(
                    f"Darwin closed the {self.service} connection unexpectedly"
                )
            self._buffer += chunk

    def _classify(self, command: str, line: str) -> str | None:
        """Return the line unless it is an ERR that should not be treated as
        this command's response. Raises for a genuine refusal."""
        if not line.startswith("ERR;"):
            return line
        _subject, code = _decode_error(line)
        if code in NOTIFICATION_CODES:
            self.unsolicited.append(line)
            return None
        return line

    def request_single(
        self,
        command: str,
        prefix: str | Sequence[str],
        *,
        timeout: float = 5.0,
        match: str | None = None,
    ) -> str:
        """Send `command` and return the one line starting with `prefix` (or any
        of several accepted prefixes), skipping unsolicited traffic. `match`
        additionally requires the line's second field to equal it, so a pushed
        update for another symbol cannot be mistaken for the answer to
        GETPOSITION."""
        prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
        self.send(command)
        deadline = time.monotonic() + timeout
        while True:
            line = self._next_line(deadline)
            if line is None:
                raise DapiTimeout(
                    f"No {' / '.join(prefixes)} response to {command} "
                    f"within {timeout:g}s"
                )
            if not line:
                continue
            checked = self._classify(command, line)
            if checked is None:
                continue
            if checked.startswith("ERR;"):
                subject, code = _decode_error(checked)
                raise DapiError(command, code, subject)
            if not any(checked.startswith(p) for p in prefixes):
                self.unsolicited.append(checked)
                continue
            if match is not None:
                fields = checked.split(";")
                if len(fields) < 2 or fields[1] != match:
                    self.unsolicited.append(checked)
                    continue
            return checked

    def request_framed(
        self,
        command: str,
        begin: str,
        end: str,
        *,
        timeout: float = 5.0,
    ) -> list[str]:
        """Send `command` and return every line between its BEGIN and END
        markers. This is the read the wrapper got wrong: the block is read to
        its terminator, so a list never comes back truncated. An
        empty-list ERR (1018/1019) yields []."""
        self.send(command)
        deadline = time.monotonic() + timeout
        started = False
        collected: list[str] = []
        while True:
            line = self._next_line(deadline)
            if line is None:
                raise DapiTimeout(
                    f"{command} did not complete within {timeout:g}s "
                    f"({'no ' + end if started else 'no ' + begin} marker)"
                )
            if not line:
                continue
            checked = self._classify(command, line)
            if checked is None:
                continue
            if checked.startswith("ERR;"):
                subject, code = _decode_error(checked)
                if code in EMPTY_LIST_CODES:
                    return []
                raise DapiError(command, code, subject)
            if not started:
                if checked.startswith(begin):
                    started = True
                else:
                    self.unsolicited.append(checked)
                continue
            if checked.startswith(end):
                return collected
            collected.append(checked)

    def request_until(
        self,
        command: str,
        terminators: Sequence[str],
        keep_prefix: str,
        *,
        timeout: float = 60.0,
    ) -> list[str]:
        """Send `command` and read until one of `terminators`, keeping the data
        lines. Used for the historical port, whose bulk responses end with an
        explicit END marker but can take a while to arrive."""
        self.send(command)
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while True:
            line = self._next_line(deadline)
            if line is None:
                raise DapiTimeout(
                    f"{command} did not reach its end marker within {timeout:g}s"
                )
            if not line:
                continue
            checked = self._classify(command, line)
            if checked is None:
                continue
            if checked.startswith("ERR;"):
                subject, code = _decode_error(checked)
                raise DapiError(command, code, subject)
            if any(checked.startswith(marker) for marker in terminators):
                return collected
            if checked.startswith(keep_prefix):
                collected.append(checked)


def parse_stock(line: str) -> dict[str, Any]:
    """STOCK;<ticker>;<ora>;<qta portafoglio>;<qta directa>;<qta negoziazione>;
    <prezzo medio>;<gain teorico>"""
    p = line.split(";")
    return {
        "symbol": p[1] if len(p) > 1 else None,
        "time": p[2] if len(p) > 2 else None,
        "quantity_portfolio": _number(p[3]) if len(p) > 3 else None,
        "quantity_directa": _number(p[4]) if len(p) > 4 else None,
        "quantity_trading": _number(p[5]) if len(p) > 5 else None,
        "average_price": _number(p[6]) if len(p) > 6 else None,
        "theoretical_gain": _number(p[7]) if len(p) > 7 else None,
        "raw": line,
    }


def parse_order(line: str) -> dict[str, Any]:
    """ORDER;<ticker>;<ora>;<id>;<operazione>;<prezzo limite>;<prezzo segnale>;
    <quantità>;<stato>"""
    p = line.split(";")
    status = p[9].strip() if len(p) > 9 else (p[8].strip() if len(p) > 8 else "")
    return {
        "symbol": p[1] if len(p) > 1 else None,
        "time": p[2] if len(p) > 2 else None,
        "order_id": p[3] if len(p) > 3 else None,
        "operation": p[4] if len(p) > 4 else None,
        "limit_price": _number(p[5]) if len(p) > 5 else None,
        "signal_price": _number(p[6]) if len(p) > 6 else None,
        "quantity": _number(p[7]) if len(p) > 7 else None,
        "status_code": status,
        "status": ORDER_STATUS.get(status, "Unknown order state"),
        "raw": line,
    }


def parse_account(line: str) -> dict[str, Any]:
    """INFOACCOUNT;<ora>;<conto>;<liquidità>;<gain euro>;<open P/L>;<equity>;<modo>

    Field names follow Directa's documentation, which for fields 4 and 5 does
    not match this build. Measured against a live account:

    - field 4 (`gain_euro`) equals the sum of the per-position
      `theoretical_gain` values from INFOSTOCKS — it is the open P&L;
    - field 5 (`open_profit_loss`) equals the portfolio's **cost basis**,
      reconciled to the cent across 15 positions.

    So neither label describes its field. `open_profit_loss` in particular is
    cost, not profit — reading it as P&L overstates it by roughly an order of
    magnitude. The names are kept to match Directa's documentation rather than
    inventing new ones, and `raw` is returned so any figure can be checked.
    portfolio_overview() derives the values independently and verifies them
    against field 5.
    """
    p = line.split(";")
    return {
        "time": p[1] if len(p) > 1 else None,
        "account_code": p[2] if len(p) > 2 else None,
        "liquidity": _number(p[3]) if len(p) > 3 else None,
        "gain_euro": _number(p[4]) if len(p) > 4 else None,
        "open_profit_loss": _number(p[5]) if len(p) > 5 else None,
        "equity": _number(p[6]) if len(p) > 6 else None,
        "mode": p[7] if len(p) > 7 else None,
        "raw": line,
    }


def parse_availability(line: str) -> dict[str, Any]:
    """AVAILABILITY;<ora>;<disp. azioni>;<disp. azioni marg.>;<disp. derivati>;
    <disp. derivati marg.>;<liquidità totale>"""
    p = line.split(";")
    return {
        "time": p[1] if len(p) > 1 else None,
        "stock_availability": _number(p[2]) if len(p) > 2 else None,
        "stock_availability_margin": _number(p[3]) if len(p) > 3 else None,
        "derivatives_availability": _number(p[4]) if len(p) > 4 else None,
        "derivatives_availability_margin": _number(p[5]) if len(p) > 5 else None,
        "total_liquidity": _number(p[6]) if len(p) > 6 else None,
        "raw": line,
    }


def parse_darwin_status(line: str) -> dict[str, Any]:
    """DARWIN_STATUS;<stato connessione>;<datafeed abilitato>;<release>

    The third field is the datafeed flag. When it is FALSE every historical
    command answers ERR 1032, which is what makes candles and ticks
    unavailable — an account entitlement, not a fault in this client.
    """
    p = line.split(";")
    connection = p[1] if len(p) > 1 else None
    datafeed = (p[2].strip().upper() if len(p) > 2 else "") == "TRUE"
    return {
        "connection_status": connection,
        "is_connected": connection == "CONN_OK",
        "datafeed_enabled": datafeed,
        "release": p[3].strip() if len(p) > 3 else None,
        "raw": line,
    }


def parse_order_ack(line: str) -> dict[str, Any]:
    """Response to an order command. Per Directa's documentation:

      TRADOK;<ticker>;<id>;<codice>;<comando>;<qta richiesta|qta eseguita>;<prezzo>;<descrizione>
      TRADERR;<ticker>;<id>;<codice errore>;<comando>;<qta>;<prezzo>;<descrizione>
      TRADCONFIRM;<ticker>;<id>;3003;<tipo operazione>;<qta>;<prezzo>;<messaggio>

    TRADCONFIRM means Darwin wants an explicit CONFORD before the order
    reaches the market, so `accepted` stays False until that second step.

    Ack codes are a 3000 series (3003 = confirmation required) and are *not*
    the 2000-series order states of an ORDER line, so they are reported as-is
    rather than decoded against the wrong table. Parsing is deliberately
    lenient: this is unverified against live Darwin — no order was ever
    submitted from here — and the documented example is internally
    inconsistent about its trailing fields.
    """
    p = line.split(";")
    kind = p[0] if p else ""
    ack: dict[str, Any] = {
        "response_type": kind,
        "accepted": kind == "TRADOK",
        "confirmation_required": kind == "TRADCONFIRM",
        "symbol": p[1] if len(p) > 1 else None,
        "order_id": p[2] if len(p) > 2 else None,
        "code": p[3].strip() if len(p) > 3 else None,
        "command": p[4] if len(p) > 4 else None,
        "price": _number(p[6]) if len(p) > 6 else None,
        "message": p[7] if len(p) > 7 else None,
        "raw": line,
    }

    # The quantity field is documented as "richiesta|eseguita" — a pair when
    # part of the order filled immediately.
    quantity = p[5] if len(p) > 5 else ""
    if "|" in quantity:
        requested, _, executed = quantity.partition("|")
        ack["quantity_requested"] = _number(requested)
        ack["quantity_executed"] = _number(executed)
    else:
        ack["quantity_requested"] = _number(quantity) if quantity else None
        ack["quantity_executed"] = None

    if kind == "TRADERR":
        ack["error_code"] = ack["code"]
        ack["error_message"] = ack["message"] or ERROR_CODES.get(ack["code"] or "", "")
    return ack


def is_percent_quoted(symbol: str) -> bool:
    """True for instruments priced as a percentage of nominal value rather than
    in euro per unit — bonds, on this market. See PERCENT_QUOTED_PREFIX."""
    return bool(symbol) and symbol.startswith(PERCENT_QUOTED_PREFIX)


def position_economics(position: dict[str, Any]) -> dict[str, Any]:
    """Derive cost, current value and current price for a parsed STOCK line.

    Darwin reports quantity, average price and theoretical gain but no current
    price, and with the datafeed disabled no quote is available either. The
    gain closes that gap: it is the position's unrealised P&L, so the current
    price follows from the average price plus the gain per unit, and the value
    from cost plus gain.

    Returns the position with the derived fields added, or with `derived: False`
    when a field was missing or non-numeric and nothing could be computed.
    """
    enriched = dict(position)
    quantity = position.get("quantity_portfolio")
    average = position.get("average_price")
    gain = position.get("theoretical_gain")
    symbol = position.get("symbol") or ""

    numeric = all(isinstance(v, (int, float)) for v in (quantity, average, gain))
    if not numeric or not quantity:
        enriched["derived"] = False
        return enriched

    # For a percent-quoted instrument, quantity is nominal value and the price
    # is a percentage of it, so the euro cost is quantity * price / 100.
    scale = 100.0 if is_percent_quoted(symbol) else 1.0
    cost = quantity * average / scale
    enriched.update(
        {
            "derived": True,
            "percent_quoted": scale != 1.0,
            "cost": round(cost, 2),
            "value": round(cost + gain, 2),
            "price_now": round(average + gain / quantity * scale, 6),
            "gain_percent": round(gain / cost * 100, 2) if cost else None,
        }
    )
    return enriched


def parse_candle(line: str) -> dict[str, Any]:
    """CANDLE;<ticker>;<data>;<ora>;<close>;<low>;<high>;<open>;<volume>

    Unverified against live data: the datafeed is disabled on the account we
    have, so every historical command returns ERR 1032. The field order comes
    from Directa's documentation.
    """
    p = line.split(";")
    return {
        "symbol": p[1] if len(p) > 1 else None,
        "date": p[2] if len(p) > 2 else None,
        "time": p[3] if len(p) > 3 else None,
        "close": _number(p[4]) if len(p) > 4 else None,
        "low": _number(p[5]) if len(p) > 5 else None,
        "high": _number(p[6]) if len(p) > 6 else None,
        "open": _number(p[7]) if len(p) > 7 else None,
        "volume": _number(p[8]) if len(p) > 8 else None,
    }


def parse_tick(line: str) -> dict[str, Any]:
    """TBT;<ticker>;<data>;<ora>;<prezzo>;<quantità>  (unverified — see
    parse_candle)"""
    p = line.split(";")
    return {
        "symbol": p[1] if len(p) > 1 else None,
        "date": p[2] if len(p) > 2 else None,
        "time": p[3] if len(p) > 3 else None,
        "price": _number(p[4]) if len(p) > 4 else None,
        "quantity": _number(p[5]) if len(p) > 5 else None,
    }


class TradingClient:
    """Darwin's trading port (default 10002): account, portfolio and orders.

    Use as a context manager. On entry the connect-time push is captured and
    FLOWPOINT is enabled so list responses carry BEGIN/END markers.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 10002,
        timeout: float = 5.0,
        startup_idle: float = 1.0,
    ) -> None:
        self._conn = _Connection(host=host, port=port, service="trading")
        self._timeout = timeout
        self._startup_idle = startup_idle
        self.startup_lines: list[str] = []
        self.flowpoint = False

    def __enter__(self) -> "TradingClient":
        self._conn.connect()
        # Darwin greets with DARWIN_STATUS then pushes the whole portfolio and
        # order list. Read it out of the way so it cannot be mistaken for the
        # response to the first command.
        self.startup_lines = self._conn.drain(idle=self._startup_idle)
        try:
            self.flowpoint = self._conn.request_single(
                "FLOWPOINT TRUE", "FLOWPOINT", timeout=self._timeout
            ).split(";")[1].strip().upper() == "TRUE"
        except (DapiError, DapiTimeout):
            self.flowpoint = False
        if not self.flowpoint:
            self._conn.close()
            raise DapiConnectionError(
                "Darwin refused FLOWPOINT TRUE, so list responses arrive without "
                "BEGIN/END markers and cannot be read reliably. Refusing to "
                "guess where a portfolio or order list ends."
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._conn.close()

    @property
    def unsolicited(self) -> list[str]:
        """Pushed lines seen so far, in arrival order — the connect-time
        snapshot plus any later position/order updates."""
        return self.startup_lines + self._conn.unsolicited

    def darwin_status(self) -> dict[str, Any]:
        line = self._conn.request_single("DARWINSTATUS", "DARWIN_STATUS", timeout=self._timeout)
        return parse_darwin_status(line)

    def account_info(self) -> dict[str, Any]:
        line = self._conn.request_single("INFOACCOUNT", "INFOACCOUNT", timeout=self._timeout)
        return parse_account(line)

    def availability(self) -> dict[str, Any]:
        line = self._conn.request_single("INFOAVAILABILITY", "AVAILABILITY", timeout=self._timeout)
        return parse_availability(line)

    def positions(self) -> list[dict[str, Any]]:
        lines = self._conn.request_framed(
            "INFOSTOCKS", "BEGIN STOCKLIST", "END STOCKLIST", timeout=self._timeout
        )
        return [parse_stock(line) for line in lines if line.startswith("STOCK;")]

    def portfolio_overview(self) -> dict[str, Any]:
        """Positions with derived value and price, plus totals checked against
        Darwin's own figures.

        The check is the point. Deriving a value means assuming things — that
        theoretical gain is unrealised P&L, that an `M.` symbol is quoted as a
        percentage of nominal — and a wrong assumption on a bond is a 100x
        error in a euro figure. So the derived cost is compared against the
        portfolio cost Darwin reports in INFOACCOUNT, and the result carries
        `reconciled` plus the residual. Do not present the totals as
        authoritative when `reconciled` is False.
        """
        positions = [position_economics(p) for p in self.positions()]
        account = self.account_info()

        derived = [p for p in positions if p.get("derived")]
        cost = round(sum(p["cost"] for p in derived), 2)
        value = round(sum(p["value"] for p in derived), 2)
        gain = round(sum(p["theoretical_gain"] for p in derived), 2)

        # Field 5 of INFOACCOUNT matched the summed cost basis exactly on the
        # account we measured, despite its documented label; see parse_account.
        reported_cost = account.get("open_profit_loss")
        residual = None
        reconciled = None
        if isinstance(reported_cost, (int, float)):
            residual = round(cost - reported_cost, 2)
            # A cent of rounding is expected; a bond mishandled by 100x is not.
            reconciled = abs(residual) < max(1.0, abs(reported_cost) * 0.0001)

        return {
            "positions": positions,
            "count": len(positions),
            "totals": {
                "cost": cost,
                "value": value,
                "gain": gain,
                "gain_percent": round(gain / cost * 100, 2) if cost else None,
                "liquidity": account.get("liquidity"),
                "equity_reported": account.get("equity"),
                "equity_derived": _sum_or_none(value, account.get("liquidity")),
                # Darwin's equity sits a little above positions + cash. On the
                # account measured the gap was a few hundred euro on a real portfolio,
                # consistent with accrued bond interest, which Darwin counts in
                # equity but does not report per position. Left visible rather
                # than reconciled away, so the two figures are never presented
                # as if they should match exactly.
                "equity_gap": _difference_or_none(
                    account.get("equity"), _sum_or_none(value, account.get("liquidity"))
                ),
            },
            "reconciliation": {
                "reconciled": reconciled,
                "derived_cost": cost,
                "cost_reported_by_darwin": reported_cost,
                "residual": residual,
                "positions_not_derived": len(positions) - len(derived),
            },
            "account": account,
        }

    def position(self, symbol: str) -> dict[str, Any]:
        line = self._conn.request_single(
            f"GETPOSITION {symbol}", "STOCK;", timeout=self._timeout, match=symbol
        )
        return parse_stock(line)

    def orders(self, *, pending_only: bool = False, symbol: str | None = None) -> list[dict[str, Any]]:
        if pending_only:
            command = "ORDERLISTPENDING"
        elif symbol:
            command = f"ORDERLIST {symbol}"
        else:
            command = "ORDERLIST"
        lines = self._conn.request_framed(
            command, "BEGIN ORDERLIST", "END ORDERLIST", timeout=self._timeout
        )
        return [parse_order(line) for line in lines if line.startswith("ORDER;")]

    # --- Order mutation ---------------------------------------------------
    # These send real orders to a real account. Unlike the read commands above
    # they are NOT verified against live Darwin: the test account is PROD with
    # open positions, so no order was ever submitted. Command syntax follows
    # Directa's documentation, corroborated by the `operation` field of ORDER
    # lines we did observe (VENAZ for a GUI-placed sell limit).

    #: Any of these can answer an order command.
    _ORDER_ACKS = ("TRADOK;", "TRADERR;", "TRADCONFIRM;")

    def _order_command(self, command: str) -> dict[str, Any]:
        line = self._conn.request_single(command, self._ORDER_ACKS, timeout=self._timeout)
        return parse_order_ack(line)

    def _submit(self, command: str, order_id: str, confirm: bool) -> dict[str, Any]:
        """Send an order command and, when Darwin asks for confirmation, answer
        it on this same connection.

        The connection matters. Darwin replies to a limit order with
        TRADCONFIRM, and the order is not on the market until CONFORD is sent
        for the same id — but a pending confirmation does not outlive the
        connection that produced it. Confirming from a fresh session is refused
        with ERR 1010, verified against a live account. So an order cannot be
        submitted and confirmed by two separate calls that each open their own
        connection; both steps have to happen here.

        Returns both stages and, most importantly, `on_market`: whether the
        order actually reached the market. Never infer that from the absence of
        an error — an unconfirmed TRADCONFIRM is a successful exchange that
        placed nothing.
        """
        submitted = self._order_command(command)

        if not submitted.get("confirmation_required"):
            # Darwin accepted or rejected outright. If confirmation prompts are
            # switched off in Darwin, a plain TRADOK here means the order is
            # already live, whatever `confirm` asked for.
            return {
                "submitted": submitted,
                "confirmed": None,
                "on_market": bool(submitted.get("accepted")),
                "confirmation_was_required": False,
            }

        if not confirm:
            return {
                "submitted": submitted,
                "confirmed": None,
                "on_market": False,
                "confirmation_was_required": True,
            }

        confirmed = self._order_command(f"CONFORD {order_id}")
        return {
            "submitted": submitted,
            "confirmed": confirmed,
            "on_market": bool(confirmed.get("accepted")),
            "confirmation_was_required": True,
        }

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_id: str,
        confirm: bool = True,
    ) -> dict[str, Any]:
        """ACQAZ/VENAZ <id>,<ticker>,<quantità>,<prezzo>

        With confirm=False the order is submitted but left unconfirmed, so
        nothing reaches the market and Darwin's pre-trade disclosure — the
        instrument's full name, the amount, the commission that would apply —
        comes back in the acknowledgement. That is the only way this API
        reveals commissions; no command reports them.
        """
        prefix = {"buy": "ACQAZ", "sell": "VENAZ"}[side.lower()]
        return self._submit(
            f"{prefix} {order_id},{symbol},{quantity},{price}", order_id, confirm
        )

    def modify_order(
        self,
        order_id: str,
        price: float,
        signal_price: float | None = None,
        confirm: bool = True,
    ) -> dict[str, Any]:
        """MODORD <id>,<prezzo>[,<prezzo segnale>]"""
        command = f"MODORD {order_id},{price}"
        if signal_price is not None:
            command += f",{signal_price}"
        return self._submit(command, order_id, confirm)

    def confirm_order(self, order_id: str) -> dict[str, Any]:
        """CONFORD <id> — the second step after a TRADCONFIRM.

        Only usable on the connection that received the TRADCONFIRM, which is
        why it is not exposed as a tool of its own: see _submit.
        """
        return self._order_command(f"CONFORD {order_id}")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """REVORD <id>"""
        return self._order_command(f"REVORD {order_id}")

    def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        """REVALL <ticker>"""
        return self._order_command(f"REVALL {symbol}")


class HistoricalClient:
    """Darwin's historical-data port (default 10003): candles and ticks.

    Every command here currently answers ERR 1032 (datafeed not enabled) on our
    account, so the read path is implemented from Directa's documentation and
    is not verified against live data. The error is surfaced as a DapiError
    rather than swallowed.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 10003,
        timeout: float = 60.0,
        startup_idle: float = 0.5,
    ) -> None:
        self._conn = _Connection(host=host, port=port, service="historical")
        self._timeout = timeout
        self._startup_idle = startup_idle

    def __enter__(self) -> "HistoricalClient":
        self._conn.connect()
        self._conn.drain(idle=self._startup_idle)  # DARWIN_STATUS greeting
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._conn.close()

    def candles(self, symbol: str, days: int, period_seconds: int) -> list[dict[str, Any]]:
        lines = self._conn.request_until(
            f"CANDLE {symbol} {days} {period_seconds}",
            ("END CANDLES",),
            "CANDLE;",
            timeout=self._timeout,
        )
        return [parse_candle(line) for line in lines]

    def candles_range(
        self, symbol: str, start: str, end: str, period_seconds: int
    ) -> list[dict[str, Any]]:
        lines = self._conn.request_until(
            f"CANDLERANGE {symbol} {start} {end} {period_seconds}",
            ("END CANDLES",),
            "CANDLE;",
            timeout=self._timeout,
        )
        return [parse_candle(line) for line in lines]

    def ticks(self, symbol: str, days: int) -> list[dict[str, Any]]:
        lines = self._conn.request_until(
            f"TBT {symbol} {days}", ("END TBT",), "TBT;", timeout=self._timeout
        )
        return [parse_tick(line) for line in lines]


def check_ports(host: str, ports: Iterable[tuple[str, int]]) -> dict[str, Any]:
    """Raw TCP reachability check, independent of the dAPI conversation. Use it
    to tell "Darwin is not running" apart from "Darwin refused the command"."""
    results: dict[str, Any] = {}
    for label, port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            sock.connect((host, port))
            results[label] = {"host": host, "port": port, "reachable": True}
        except OSError as exc:
            results[label] = {
                "host": host,
                "port": port,
                "reachable": False,
                "error": str(exc),
            }
        finally:
            sock.close()
    return results
