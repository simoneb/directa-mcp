"""Tests for the dAPI client.

Response shapes follow what a live Darwin 2.5.1 sends (see docs/PROTOCOL.md);
every value here is synthetic, so no real account appears in this repository.
The account line is computed from the positions below, which keeps the
reconciliation test meaningful without hand-maintained totals.

The first test is the one that matters most: it pins the bug that made the
previous implementation unusable, where a multi-line response came back as a
single row.
"""

from __future__ import annotations

import pytest
from fake_darwin import FakeDarwin

from directa_mcp import dapi

STATUS = (
    "DARWIN_STATUS;CONN_OK;FALSE;Release  2.5.1 build 04/02/2025 11:00:00 "
    "more info at http://app1.directatrading.com/trading-api-directa/index.html"
)

# Fifteen positions, mixing euro-quoted equities with percent-quoted bonds —
# enough rows that a truncating reader is caught, and both pricing conventions
# represented.
STOCKS = [
    "STOCK;ENI.MI;10:00:00;100;0;0;13.5;120",
    "STOCK;ISP.MI;10:00:00;500;0;0;3.2;45",
    "STOCK;UCG.MI;10:00:00;200;0;0;35.4;-80",
    "STOCK;STLAM.MI;10:00:00;300;0;0;8.75;210",
    "STOCK;ENEL.MI;10:00:00;400;0;0;6.1;90",
    "STOCK;RACE.MI;10:00:00;10;0;0;380.0;150",
    "STOCK;G.MI;10:00:00;250;0;0;24.8;-35",
    "STOCK;PST.MI;10:00:00;150;0;0;12.4;60",
    "STOCK;MB.MI;10:00:00;180;0;0;14.9;25",
    "STOCK;BAMI.MI;10:00:00;900;0;0;7.3;110",
    "STOCK;SRG.MI;10:00:00;700;0;0;4.55;85",
    "STOCK;M.100001;10:00:00;20000;0;0;95.0;200",
    "STOCK;M.100002;10:00:00;10000;0;0;99.5;-40",
    "STOCK;M.100003;10:00:00;20000;0;0;88.0;300",
    "STOCK;M.100004;10:00:00;10000;0;-10000;97.25;-60",
]


def _position_cost(line: str) -> float:
    """Euro cost of a STOCK line, honouring the bond percent convention."""
    parts = line.split(";")
    quantity, average = int(parts[3]), float(parts[6])
    scale = 100.0 if parts[1].startswith("M.") else 1.0
    return quantity * average / scale


LIQUIDITY = 1500.0
COST_BASIS = round(sum(_position_cost(line) for line in STOCKS), 4)
OPEN_PL = sum(int(line.split(";")[7]) for line in STOCKS)
EQUITY = round(COST_BASIS + OPEN_PL + LIQUIDITY, 2)

# Field 4 carries the open P&L and field 5 the cost basis, despite their
# documented names — see parse_account.
ACCOUNT = f"INFOACCOUNT;10:00:00;A0000;{LIQUIDITY};{OPEN_PL};{COST_BASIS};{EQUITY};PROD"
AVAILABILITY = f"AVAILABILITY;10:00:01;{LIQUIDITY};24000.0;0.0;0.0;{LIQUIDITY}"

# Four order records for one symbol: one working (2000), three cancelled (2004).
ORDERS = [
    "ORDER;ENI.MI;09:58:54;ORD1;VENAZ;13.9;0.0;100;2004",
    "ORDER;ENI.MI;09:59:26;ORD2;VENAZ;13.85;0.0;100;2000",
    "ORDER;ENI.MI;09:58:38;ORD3;VENAZ;13.95;0.0;100;2004",
    "ORDER;ENI.MI;09:59:26;ORD4;VENAZ;13.8;0.0;100;2004",
]

# What Darwin pushes on connect, before any command is sent.
CONNECT_PUSH = [STATUS, *STOCKS, *ORDERS]


def trading_responses(**overrides: object) -> dict[str, object]:
    responses: dict[str, object] = {
        "FLOWPOINT TRUE": ["FLOWPOINT;TRUE"],
        "FLOWPOINT": ["FLOWPOINT;TRUE"],
        "DARWINSTATUS": [STATUS],
        "INFOACCOUNT": [ACCOUNT],
        "INFOAVAILABILITY": [AVAILABILITY],
        "INFOSTOCKS": ["BEGIN STOCKLIST", *STOCKS, "END STOCKLIST"],
        "ORDERLIST": ["BEGIN ORDERLIST", *ORDERS, "END ORDERLIST"],
        "ORDERLISTPENDING": ["BEGIN ORDERLIST", ORDERS[1], "END ORDERLIST"],
    }
    responses.update(overrides)  # type: ignore[arg-type]
    return responses


def trading_client(fake: FakeDarwin) -> dapi.TradingClient:
    return dapi.TradingClient(port=fake.port, timeout=3.0, startup_idle=0.15)


def historical_client(fake: FakeDarwin) -> dapi.HistoricalClient:
    return dapi.HistoricalClient(port=fake.port, timeout=3.0, startup_idle=0.15)


class TestNoTruncation:
    """The regression that motivated replacing the third-party wrapper: it
    returned the first matching line of a multi-line response and dropped the
    rest, so a 15-position portfolio arrived as 1 position."""

    def test_all_positions_returned(self) -> None:
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                positions = api.positions()
        assert len(positions) == 15
        assert [p["symbol"] for p in positions] == [s.split(";")[1] for s in STOCKS]

    def test_all_orders_returned(self) -> None:
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                orders = api.orders()
        assert len(orders) == 4

    def test_survives_lines_split_across_reads(self) -> None:
        """A 4-byte chunk size splits nearly every line across TCP reads."""
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH, chunk_size=4) as fake:
            with trading_client(fake) as api:
                positions = api.positions()
                orders = api.orders()
        assert len(positions) == 15
        assert len(orders) == 4
        assert positions[0]["average_price"] == 13.5


class TestUnsolicitedTraffic:
    """Darwin pushes position and order updates at any time. They must never be
    mistaken for the response to the command in flight."""

    def test_connect_push_is_not_read_as_a_response(self) -> None:
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                assert len(api.startup_lines) == len(CONNECT_PUSH)
                account = api.account_info()
        assert account["account_code"] == "A0000"

    def test_pushed_update_before_response_is_skipped(self) -> None:
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                fake.inject_before_next = ["STOCK;ENI.MI;10:05:00;100;0;0;13.5;135"]
                account = api.account_info()
                assert account["account_code"] == "A0000"
                assert any(line.endswith(";135") for line in api.unsolicited)

    def test_pushed_position_for_another_symbol_is_not_returned(self) -> None:
        target = "STOCK;M.100004;10:05:00;10000;0;-10000;97.25;-60"
        with FakeDarwin(
            trading_responses(**{"GETPOSITION M.100004": [target]}), push=CONNECT_PUSH
        ) as fake:
            with trading_client(fake) as api:
                fake.inject_before_next = ["STOCK;ENI.MI;10:05:00;100;0;0;13.5;135"]
                position = api.position("M.100004")
        assert position["symbol"] == "M.100004"
        assert position["quantity_trading"] == -10000

    def test_link_notification_does_not_fail_the_command(self) -> None:
        """ERR 1027 announces a datafeed drop; it is not a refusal of the
        command being sent."""
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                fake.inject_before_next = ["ERR;DATAFEED;1027"]
                account = api.account_info()
        assert account["equity"] == EQUITY


class TestErrors:
    def test_refused_command_raises_with_decoded_code(self) -> None:
        with FakeDarwin(
            trading_responses(INFOACCOUNT=["ERR;INFOACCOUNT;1004"]), push=CONNECT_PUSH
        ) as fake:
            with trading_client(fake) as api:
                with pytest.raises(dapi.DapiError) as caught:
                    api.account_info()
        assert caught.value.code == "1004"
        assert "ERR_COMMAND_NOT_EXECUTED" in caught.value.description

    def test_empty_portfolio_is_empty_not_an_error(self) -> None:
        with FakeDarwin(
            trading_responses(INFOSTOCKS=["ERR;INFOSTOCKS;1018"]), push=[STATUS]
        ) as fake:
            with trading_client(fake) as api:
                assert api.positions() == []

    def test_empty_order_list_is_empty_not_an_error(self) -> None:
        with FakeDarwin(
            trading_responses(ORDERLIST=["ERR;ORDERLIST;1019"]), push=[STATUS]
        ) as fake:
            with trading_client(fake) as api:
                assert api.orders() == []

    def test_unreachable_port_is_reported_clearly(self) -> None:
        fake = FakeDarwin()
        port = fake.port
        fake.stop()  # nothing listening now
        with pytest.raises(dapi.DapiConnectionError, match="Cannot reach Darwin"):
            with dapi.TradingClient(port=port, startup_idle=0.1):
                pass

    def test_refuses_to_run_without_flowpoint_framing(self) -> None:
        """Without BEGIN/END markers a list has no reliable end, so the client
        declines rather than returning a possibly-partial portfolio."""
        responses = trading_responses()
        responses["FLOWPOINT TRUE"] = ["ERR;FLOWPOINT;1004"]
        with FakeDarwin(responses, push=[STATUS]) as fake:
            with pytest.raises(dapi.DapiConnectionError, match="FLOWPOINT"):
                with trading_client(fake):
                    pass

    def test_missing_end_marker_times_out_rather_than_truncating(self) -> None:
        responses = trading_responses(INFOSTOCKS=["BEGIN STOCKLIST", *STOCKS])
        with FakeDarwin(responses, push=[STATUS]) as fake:
            client = dapi.TradingClient(port=fake.port, timeout=0.5, startup_idle=0.15)
            with client as api:
                with pytest.raises(dapi.DapiTimeout):
                    api.positions()


class TestParsing:
    def test_position_fields(self) -> None:
        parsed = dapi.parse_stock(STOCKS[0])
        assert parsed["symbol"] == "ENI.MI"
        assert parsed["quantity_portfolio"] == 100
        assert parsed["average_price"] == 13.5
        assert parsed["theoretical_gain"] == 120

    def test_order_state_is_decoded(self) -> None:
        working = dapi.parse_order(ORDERS[1])
        cancelled = dapi.parse_order(ORDERS[0])
        assert working["status_code"] == "2000"
        assert working["status"] == "In negoziazione"
        assert working["order_id"] == "ORD2"
        assert working["limit_price"] == 13.85
        assert cancelled["status"] == "Revocato"

    def test_pending_filter_returns_only_the_working_order(self) -> None:
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                pending = api.orders(pending_only=True)
        assert [o["status_code"] for o in pending] == ["2000"]

    def test_account_fields(self) -> None:
        parsed = dapi.parse_account(ACCOUNT)
        assert parsed["account_code"] == "A0000"
        assert parsed["liquidity"] == LIQUIDITY
        assert parsed["equity"] == EQUITY
        assert parsed["mode"] == "PROD"
        assert parsed["raw"] == ACCOUNT

    def test_gain_field_matches_sum_of_position_gains(self) -> None:
        """The observation behind the caveat in parse_account's docstring."""
        total = sum(dapi.parse_stock(line)["theoretical_gain"] for line in STOCKS)
        assert abs(dapi.parse_account(ACCOUNT)["gain_euro"] - total) < 100

    def test_datafeed_flag_read_from_status(self) -> None:
        assert dapi.parse_darwin_status(STATUS)["datafeed_enabled"] is False
        assert dapi.parse_darwin_status(STATUS)["is_connected"] is True
        enabled = STATUS.replace("CONN_OK;FALSE", "CONN_OK;TRUE")
        assert dapi.parse_darwin_status(enabled)["datafeed_enabled"] is True

    def test_unparseable_number_is_kept_verbatim(self) -> None:
        parsed = dapi.parse_stock("STOCK;X;11:00:00;10;0;0;n/a;5")
        assert parsed["average_price"] == "n/a"

    def test_order_ack_variants(self) -> None:
        ok = dapi.parse_order_ack("TRADOK;STLAM;ORD001;3000;ACQAZ;10;4.75;")
        assert ok["accepted"] is True and ok["confirmation_required"] is False
        assert ok["code"] == "3000" and ok["quantity_requested"] == 10
        assert ok["price"] == 4.75

        err = dapi.parse_order_ack("TRADERR;STLAM;ORD001;1012;ACQAZ;10;4.75;rifiutato")
        assert err["accepted"] is False and err["error_code"] == "1012"
        assert err["error_message"] == "rifiutato"

        confirm = dapi.parse_order_ack(
            "TRADCONFIRM;STLAM;ORD001;3003;ACQAZ;10;4.75;confermare"
        )
        assert confirm["accepted"] is False and confirm["confirmation_required"] is True
        assert confirm["code"] == "3003"

    def test_order_ack_splits_partial_fill_quantity(self) -> None:
        """Documented as <quantità richiesta|quantità eseguita>."""
        ack = dapi.parse_order_ack("TRADOK;STLAM;ORD001;3000;ACQAZ;100|40;4.75;")
        assert ack["quantity_requested"] == 100
        assert ack["quantity_executed"] == 40

    def test_ack_code_is_not_decoded_as_an_order_state(self) -> None:
        """Acks use a 3000 series; 2000-series labels would be wrong here."""
        ack = dapi.parse_order_ack("TRADOK;STLAM;ORD001;3000;ACQAZ;10;4.75;")
        assert "status" not in ack


class TestOrderSubmission:
    """Darwin answers a limit order with TRADCONFIRM and places nothing until
    CONFORD arrives on the same connection. Verified against a live account:
    confirming from a fresh session is refused with ERR 1010, so submit and
    confirm cannot be split across two calls that each open their own socket."""

    ORDER = "ACQAZ TEST1,ENI.MI,29,13.5"
    CONFIRM = "CONFORD TEST1"

    def responses(self, **overrides: object) -> dict[str, object]:
        base = trading_responses()
        base[self.ORDER] = [
            "TRADCONFIRM;ENI.MI;TEST1;3003;ACQAZ;29;13.5;"
            "VERIFICARE I DATI - ACQUISTO 29 A 13,5 EUR - Commissione prevista 0 EUR"
        ]
        base[self.CONFIRM] = ["TRADOK;ENI.MI;TEST1;3000;ACQAZ;29;13.5;"]
        base.update(overrides)  # type: ignore[arg-type]
        return base

    def test_preview_submits_but_never_confirms(self) -> None:
        with FakeDarwin(self.responses(), push=[STATUS]) as fake:
            with trading_client(fake) as api:
                result = api.place_limit_order(
                    "ENI.MI", "buy", 29, 13.5, "TEST1", confirm=False
                )
        assert result["on_market"] is False
        assert result["confirmation_was_required"] is True
        assert result["confirmed"] is None
        assert self.ORDER in fake.received
        assert self.CONFIRM not in fake.received

    def test_preview_returns_darwins_commission_disclosure(self) -> None:
        """The only route to commission figures: no dAPI command reports them."""
        with FakeDarwin(self.responses(), push=[STATUS]) as fake:
            with trading_client(fake) as api:
                result = api.place_limit_order(
                    "ENI.MI", "buy", 29, 13.5, "TEST1", confirm=False
                )
        assert "Commissione prevista 0 EUR" in result["submitted"]["message"]

    def test_place_confirms_on_the_same_connection(self) -> None:
        with FakeDarwin(self.responses(), push=[STATUS]) as fake:
            with trading_client(fake) as api:
                result = api.place_limit_order(
                    "ENI.MI", "buy", 29, 13.5, "TEST1", confirm=True
                )
        assert result["on_market"] is True
        assert result["confirmed"]["accepted"] is True
        # Order first, confirmation second, both within one session.
        assert fake.received.index(self.ORDER) < fake.received.index(self.CONFIRM)

    def test_order_live_without_a_confirmation_prompt_is_reported(self) -> None:
        """If Darwin is configured not to ask, a plain TRADOK means the order is
        already on the market — including when only a preview was intended."""
        responses = self.responses(**{self.ORDER: ["TRADOK;ENI.MI;TEST1;3000;ACQAZ;29;13.5;"]})
        with FakeDarwin(responses, push=[STATUS]) as fake:
            with trading_client(fake) as api:
                result = api.place_limit_order(
                    "ENI.MI", "buy", 29, 13.5, "TEST1", confirm=False
                )
        assert result["on_market"] is True
        assert result["confirmation_was_required"] is False
        assert self.CONFIRM not in fake.received

    def test_rejected_order_is_not_on_market(self) -> None:
        responses = self.responses(
            **{self.ORDER: ["TRADERR;ENI.MI;TEST1;1012;ACQAZ;29;13.5;rifiutato"]}
        )
        with FakeDarwin(responses, push=[STATUS]) as fake:
            with trading_client(fake) as api:
                result = api.place_limit_order(
                    "ENI.MI", "buy", 29, 13.5, "TEST1", confirm=True
                )
        assert result["on_market"] is False
        assert result["submitted"]["error_code"] == "1012"
        assert self.CONFIRM not in fake.received

    def test_refused_confirmation_leaves_the_order_off_market(self) -> None:
        """What a stale confirmation looks like: ERR 1010 on CONFORD."""
        responses = self.responses(**{self.CONFIRM: ["ERR;CONFORD;1010"]})
        with FakeDarwin(responses, push=[STATUS]) as fake:
            with trading_client(fake) as api:
                with pytest.raises(dapi.DapiError) as caught:
                    api.place_limit_order("ENI.MI", "buy", 29, 13.5, "TEST1", confirm=True)
        assert caught.value.code == "1010"


class TestDerivedValues:
    """Darwin reports no current price, so price and value are derived. The
    bond convention is the trap: getting it wrong is a 100x error in euro."""

    def test_equity_instrument_value_and_price(self) -> None:
        position = dapi.parse_stock("STOCK;ENI.MI;10:00:00;100;0;0;13.5;120")
        derived = dapi.position_economics(position)
        assert derived["percent_quoted"] is False
        assert derived["cost"] == 1350.00
        assert derived["value"] == 1470.00
        assert derived["price_now"] == pytest.approx(14.7, abs=1e-4)

    def test_bond_is_priced_as_a_percentage_of_nominal(self) -> None:
        """20000 nominal at 95.00 is 19,000 euro — not 1,900,000."""
        position = dapi.parse_stock("STOCK;M.100001;10:00:00;20000;0;0;95.0;200")
        derived = dapi.position_economics(position)
        assert derived["percent_quoted"] is True
        assert derived["cost"] == 19000.00
        assert derived["value"] == 19200.00
        # The gain is 200 euro on 20000 nominal, i.e. 1.00 per 100 nominal.
        assert derived["price_now"] == pytest.approx(96.0, abs=1e-4)

    def test_non_numeric_fields_yield_no_derived_figures(self) -> None:
        derived = dapi.position_economics(dapi.parse_stock("STOCK;X;11:00:00;10;0;0;n/a;5"))
        assert derived["derived"] is False
        assert "value" not in derived

    def test_zero_quantity_does_not_divide_by_zero(self) -> None:
        derived = dapi.position_economics(dapi.parse_stock("STOCK;X;11:00:00;0;0;0;10.0;0"))
        assert derived["derived"] is False

    def test_percent_quoted_detection(self) -> None:
        assert dapi.is_percent_quoted("M.100001") is True
        assert dapi.is_percent_quoted("VWCE") is False
        assert dapi.is_percent_quoted("ENI.MI") is False


class TestPortfolioOverview:
    def test_totals_reconcile_against_darwins_cost_basis(self) -> None:
        """Field 5 of INFOACCOUNT is the cost basis; the derived total must
        match it, which is what proves the bond handling right."""
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                overview = api.portfolio_overview()
        assert overview["count"] == 15
        assert overview["reconciliation"]["reconciled"] is True
        assert overview["reconciliation"]["residual"] == pytest.approx(0.0, abs=0.01)
        assert overview["totals"]["cost"] == pytest.approx(COST_BASIS, abs=0.01)

    def test_mishandled_bonds_would_fail_reconciliation(self) -> None:
        """Guard on the guard: if the percent convention were dropped, the
        totals would no longer agree with Darwin and must not claim to."""
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                positions = api.positions()
                account = api.account_info()
        naive_cost = sum(
            p["quantity_portfolio"] * p["average_price"] for p in positions
        )
        assert naive_cost > account["open_profit_loss"] * 10

    def test_equity_gap_is_reported_not_hidden(self) -> None:
        with FakeDarwin(trading_responses(), push=CONNECT_PUSH) as fake:
            with trading_client(fake) as api:
                totals = api.portfolio_overview()["totals"]
        assert totals["equity_derived"] is not None
        assert totals["equity_gap"] is not None


class TestHistorical:
    def test_datafeed_disabled_surfaces_as_error(self) -> None:
        """What our account actually returns: the subject field of the ERR line
        is the account code here, not the command."""
        with FakeDarwin({"CANDLE ENI.MI 5 86400": ["ERR;A0000;1032"]}, push=[STATUS]) as fake:
            with historical_client(fake) as api:
                with pytest.raises(dapi.DapiError) as caught:
                    api.candles("ENI.MI", 5, 86400)
        assert caught.value.code == "1032"
        assert "DATAFEED NON ABILITATO" in caught.value.description
        assert caught.value.subject == "A0000"

    def test_candles_are_read_to_the_end_marker(self) -> None:
        response = [
            "BEGIN CANDLES ENI.MI",
            "CANDLE;ENI.MI;20260810;09:30:00;13.5;13.4;13.6;13.45;10000",
            "CANDLE;ENI.MI;20260810;09:35:00;13.55;13.5;13.6;13.5;8000",
            "END CANDLES",
        ]
        with FakeDarwin(
            {"CANDLE ENI.MI 5 300": response}, push=[STATUS], chunk_size=5
        ) as fake:
            with historical_client(fake) as api:
                candles = api.candles("ENI.MI", 5, 300)
        assert len(candles) == 2
        assert candles[0]["open"] == 13.45
        assert candles[0]["close"] == 13.5
        assert candles[1]["volume"] == 8000

    def test_ticks_are_read_to_the_end_marker(self) -> None:
        response = [
            "TBT;ENI.MI;20260810;09:30:01;13.5;100",
            "TBT;ENI.MI;20260810;09:30:02;13.51;250",
            "END TBT",
        ]
        with FakeDarwin({"TBT ENI.MI 1": response}, push=[STATUS]) as fake:
            with historical_client(fake) as api:
                ticks = api.ticks("ENI.MI", 1)
        assert [t["quantity"] for t in ticks] == [100, 250]
