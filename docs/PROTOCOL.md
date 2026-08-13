# Darwin dAPI, as observed

Notes taken while building this server against live Darwin on Windows. This is
what the platform actually did, not what documentation says it should do — where
the two disagree, that is called out. The transcripts carry real response
shapes; the values in them are synthetic.

## Ports

| Port  | Purpose (Directa's own wording) |
| ----- | ------------------------------ |
| 10001 | `DATAFEED` — real-time quotes. Not listening on our install, consistent with the entitlement being off |
| 10002 | `TRADING` — account, portfolio, orders |
| 10003 | `CHIAMATE STORICHE` — candles and ticks |
| 10004 | *"porta di servizio (utilizzata esclusivamente per le nostre gestioni)"* — internal to Directa. It answers with the same `DARWIN_STATUS` greeting, so it is easy to mistake for a usable port |

Plain TCP, one line-oriented text command per request, `\r\n` terminated,
latin-1. No handshake and no authentication: Darwin is already logged in, and
anything that can reach the port can trade.

## Darwin talks first, and keeps talking

On connect, before any command is sent, Darwin pushes a greeting and a full
account snapshot — every position, then every order:

```
DARWIN_STATUS;CONN_OK;FALSE;Release  2.5.1 build 04/02/2025 11:00:00 more info at http://...
STOCK;ENI.MI;10:00:00;100;0;0;13.5;120
STOCK;M.100001;10:00:00;20000;0;0;95.0;200
…one STOCK line per position…
ORDER;ENI.MI;09:58:54;ORD1;VENAZ;13.9;0.0;100;2004
…one ORDER line per order…
```

Position and order updates keep arriving afterwards, unprompted. A client that
reads "whatever arrives next" after sending a command will sooner or later read
someone else's mail — which is why `dapi.py` matches responses by prefix and
routes everything else to `unsolicited`.

## Commands that work, and commands that look like they should

The command set is not guessable: `INFO*` and `GET*` prefixes mix with no
pattern, and a wrong name fails with the same code as a command merely refused.

| Command                | Result |
| ---------------------- | ------ |
| `DARWINSTATUS`         | `DARWIN_STATUS;...` |
| `INFOACCOUNT`          | `INFOACCOUNT;...` |
| `INFOAVAILABILITY`     | `AVAILABILITY;...` |
| `INFOSTOCKS`           | `STOCK;...` per position |
| `GETPOSITION <sym>`    | one `STOCK;...` |
| `ORDERLIST`            | `ORDER;...` per order |
| `ORDERLIST <sym>`      | `ORDER;...` for that symbol |
| `ORDERLISTPENDING`     | `ORDER;...` for working orders only |
| `FLOWPOINT TRUE`       | `FLOWPOINT;TRUE` — see framing below |
| `GETPORTFOLIO`         | **`ERR;GETPORTFOLIO;1004`** — use `INFOSTOCKS` |
| `GETACCTINFO`          | **`ERR;GETACCTINFO;1004`** — use `INFOACCOUNT` |
| `INFOSTOCK <sym>`      | `ERR;INFOSTOCK;1004` — singular is not a command |
| `GETACCOUNT`, `GETAVAILABILITY`, `PORTFOLIO`, `INFOPOSITION`, `GETPENDINGORDERS`, `TABLEDESCRIPTION` | all `1004` |

A *nonexistent* command answers 1004 ("comando non eseguito") rather than 1003
("comando sconosciuto"), so 1004 does not distinguish "wrong name" from "right
name, refused right now".

## Framing: FLOWPOINT is not optional

By default a list response is a run of lines with nothing marking where it ends;
the only way to know you have them all is to wait for silence and hope.
`FLOWPOINT TRUE` makes Darwin wrap lists in markers:

```
> FLOWPOINT TRUE
FLOWPOINT;TRUE
> INFOSTOCKS
BEGIN STOCKLIST
STOCK;ENI.MI;10:00:00;100;0;0;13.5;120
…
END STOCKLIST
> ORDERLIST
BEGIN ORDERLIST
ORDER;ENI.MI;09:58:54;ORD1;VENAZ;13.9;0.0;100;2004
…
END ORDERLIST
```

This client enables it on connect and refuses to run without it, rather than
guessing where a portfolio ends.

One caveat found the hard way: when connections arrive in quick succession —
several tool calls back to back, each opening its own socket — Darwin sometimes
leaves `FLOWPOINT TRUE` unanswered for well past a few seconds. Two calls in ten
failed that way. Reconnecting makes it worse, since every connection replays the
whole snapshot first; asking again on the same connection clears it.

## Response formats

```
DARWIN_STATUS;<stato connessione>;<datafeed abilitato>;<release>
INFOACCOUNT;<ora>;<conto>;<liquidità>;<gain euro>;<open P/L>;<equity>;<modo>
AVAILABILITY;<ora>;<disp. azioni>;<disp. az. marg.>;<disp. deriv.>;<disp. deriv. marg.>;<liquidità totale>
STOCK;<ticker>;<ora>;<qta portafoglio>;<qta directa>;<qta negoziazione>;<prezzo medio>;<gain teorico>
ORDER;<ticker>;<ora>;<id>;<operazione>;<prezzo limite>;<prezzo segnale>;<quantità>;<stato>
```

The third field of `DARWIN_STATUS` is the **datafeed** flag, which decides
whether historical data works at all.

### An `INFOACCOUNT` field-order discrepancy

Directa documents the order as
`<LIQUIDITA'>;<GAIN EURO>;<OPEN PROFIT/LOSS>;<EQUITY>`, and the official
example, `INFOACCOUNT;12:49:11;40000;150000;1200;430;2`, is internally
implausible (equity of 2). Observed:

```
INFOACCOUNT;10:00:00;A0000;1500.0;1180;95667.0;98347.0;PROD
```

Measured against the portfolio, fields 4 and 5 are not what they are called:

- **field 4** (`1180`) is the open P&L — it equals the sum of the per-position
  `theoretical_gain` values from `INFOSTOCKS`, within the drift between reads.
- **field 5** (`95667.00`) is the portfolio's **cost basis** —
  `Σ quantity × average price`, bonds handled as percentages of nominal —
  reconciled to the cent across every position.

So `open_profit_loss` holds cost, not profit; reading it as a gain overstates it
by nearly an order of magnitude. The client keeps Directa's names rather than
inventing its own, returns the `raw` line, and derives the figures independently
in `portfolio_overview()` so field 5 serves as a cross-check.

Equity (field 6) sits slightly above cost + gain + liquidity, by a few hundred
euro — consistent with accrued bond interest, which Darwin counts in equity but
reports on no position. Unexplained, so it is surfaced as `equity_gap` rather
than smoothed over.

## Ticker formats, and the bond price convention

Three formats coexist (`ENI.MI` for Borsa Italiana equities, bare `VWCE` /
`IWDA` for ETFs, `M.100001` for bonds), and the account's own portfolio is the
reliable way to learn which applies.

The `M.` prefix carries a convention that is easy to miss and expensive to get
wrong: **bonds are quoted as a percentage of nominal value**. In

```
STOCK;M.100001;10:00:00;20000;0;0;95.0;200
```

the quantity is 20,000 of nominal and the price is 95.00% of it, so the position
is worth **19,000 €** — not 1,900,000 €. A naive `quantity × price` overstates
it 100-fold. The reconciliation against `INFOACCOUNT` field 5 confirms the
convention: applied to every bond in the portfolio it matched Darwin's own cost
figure exactly, and applied to the derived price it put that bond at 96.00 —
consistent with the market, where the naive figure would have given 95.01.

### Deriving a current price without the datafeed

Darwin reports no current price on this port, and with the datafeed disabled no
quote is available either. The theoretical gain closes the gap, being the
position's unrealised P&L:

```
price_now = average_price + (theoretical_gain / quantity) × (100 if percent-quoted else 1)
value     = quantity × average_price / (100 if percent-quoted else 1) + theoretical_gain
```

Derived this way, ETF prices land within a few cents of the quotes in Darwin's
own watchlist, and the totals reconcile against `INFOACCOUNT`.

## Order states

The last field of an `ORDER` line. A symbol accumulates several records during a
session, so the state matters more than the presence of a row: after a morning
of edits, one symbol had seven records of which exactly one was live.

| Code | Meaning |
| ---- | ------- |
| 2000 | In negoziazione |
| 2001 | Errore immissione |
| 2002 | In negoziazione dopo conferma ricevuta |
| 2003 | Eseguito |
| 2004 | Revocato |
| 2005 | In attesa di conferma |
| 2006 | Modificato |

## Errors, and the ones that are not errors

`ERR;<subject>;<code>`. The subject is the command name on port 10002 but the
**account code** on port 10003 (`ERR;A0000;1032`), so it cannot be relied on to
identify what failed. Three codes need special handling:

- **1018 / 1019** — empty stock list / empty order list. An empty result, not a
  failure; an account with no positions must not look like a broken connection.
- **1024–1028** — asynchronous notices about the trading or datafeed link
  dropping and reloading. They arrive unprompted and must not be attributed to
  whatever command happens to be in flight.
- **1032** — the datafeed entitlement, below.

## Historical data needs an entitlement

Every command on port 10003 returned `ERR;<account>;1032` — *"DATAFEED NON
ABILITATO — Quotazioni non abilitate"* — regardless of symbol, date range or
candle size:

```
> CANDLE ENI.MI 5 86400                            → ERR;A0000;1032
> TBT ENI.MI 1                                     → ERR;A0000;1032
> CANDLE ENI.MI 20260810093000 20260811173000 300  → ERR;A0000;1032
```

This matches the `FALSE` datafeed flag in `DARWIN_STATUS`. It is an account
entitlement for real-time quotes, not a protocol or client problem — check
`get_darwin_status().datafeed_enabled` before blaming anything else.

Consequently the candle and tick formats come from documentation and are
**unverified**:

```
CANDLE;<ticker>;<data>;<ora>;<close>;<low>;<high>;<open>;<volume>
TBT;<ticker>;<data>;<ora>;<prezzo>;<quantità>
```

Bulk responses end with `END CANDLES` / `END TBT`; candles are additionally
preceded by `BEGIN CANDLES <ticker>`.

## Orders are a two-step exchange, bound to the connection

Verified against a live account. `ACQAZ` does **not** place an order: Darwin
answers `TRADCONFIRM` with code `3003`, and the order reaches the market only
once `CONFORD` is sent for the same id.

```
> ACQAZ MCP1700000000,<ticker>,<qta>,<prezzo>
TRADCONFIRM;<ticker>;MCP1700000000;3003;ACQAZ;<qta>;<prezzo>;LA INVITIAMO A VERIFICARE I DATI...
```

Until then nothing exists: `ORDERLIST` for the symbol returns no rows, the
position is unchanged, liquidity untouched.

The crucial part is that **a pending confirmation does not outlive its
connection**. `CONFORD` on a new socket is refused:

```
> CONFORD MCP1700000000      (new connection)
ERR;CONFORD;1010             ERR_TRADING_CMD_ERROR
```

Whether it is bound to the connection or simply expires after a few minutes is
not distinguished by this observation — both were true of the attempt. Either
way, an order cannot be submitted and confirmed by two separate exchanges that
each open their own connection: a client that opens a connection per call, as
this one does for reads, has to do both steps inside a single call.

## Commissions: only obtainable by submitting an order

No dAPI command reports commissions. Verified by probing `INFOCOMMISSION`,
`INFOCOMMISSIONS`, `GETCOMMISSION`, `COMMISSION`, `INFOFEES`, `GETFEES`,
`INFOCOSTS`, `INFOTARIFFE`, `INFOCONTRACT`, `INFOTRADES` and `TRADELIST` — all
answer `1004` — and confirmed by Directa's documentation, which describes no
such command on any port.

The figures are reachable anyway, because `TRADCONFIRM` carries Darwin's full
pre-trade disclosure: the instrument's matched name and market, the amount, the
commission that would apply, the threshold rule behind it, and any
conflict-of-interest note. A buy above the issuer's zero-commission threshold
returned, in the message field:

```
LA INVITIAMO A VERIFICARE I DATI INSERITI PRIMA DI CONFERMARE L'INVIO
DELL'ORDINE DI: ACQUISTO <nome completo strumento>, <mercato> - QUANTITA' <n>
PREZZO <p> EUR  IMPORTO <controvalore> EUR  Commissione prevista 0 EUR
Per ordini con controvalore eseguito pari o superiore a 1.000 EURO in acquisto
non viene applicata commissione: <emittente> retrocede a Directa una fee fino
ad un massimo di 7 euro ad eseguito. Potrebbe quindi verificarsi un conflitto
d'interesse
```

Verbatim apart from the placeholders. Note what it volunteers beyond the fee:
the market the order would route to, and that the issuer pays Directa a rebate
per execution.

So a submit deliberately left unconfirmed is a genuine pre-trade quote — it
discloses the cost and places nothing. Two caveats: the threshold applies to the
**executed** value, not the submitted one, so a partial fill can fall below it;
and this is only safe while Darwin is configured to ask for confirmation, since
a `TRADOK` instead means the order is already live.

## Order commands

Syntax from Directa's
[official dAPI documentation](https://app1.directatrading.com/trading-api-directa/index.html),
corroborated by the `operation` field of `ORDER` lines placed through Darwin's
own UI (`VENAZ` for a sell limit).

```
ACQAZ <id>,<ticker>,<quantità>,<prezzo>     buy limit
VENAZ <id>,<ticker>,<quantità>,<prezzo>     sell limit
ACQMARKET / VENMARKET <id>,<ticker>,<quantità>
MODORD <id>,<prezzo>[,<prezzo segnale>]     modify
REVORD <id>                                 cancel one
REVALL <ticker>                             cancel all for a symbol
CONFORD <id>                                confirm a TRADCONFIRM
```

The order id is supplied by the caller, not assigned by Darwin — orders placed
through the UI simply get sequential `ORD1`, `ORD2`, … ids.

Acknowledgements:

```
TRADOK;<ticker>;<id>;<codice>;<comando>;<qta richiesta|qta eseguita>;<prezzo>;<descrizione>
TRADERR;<ticker>;<id>;<codice errore>;<comando>;<qta>;<prezzo>;<descrizione>
TRADCONFIRM;<ticker>;<id>;3003;<tipo operazione>;<qta>;<prezzo>;<messaggio>
```

- Ack codes are a **3000 series**, distinct from the 2000-series order states of
  an `ORDER` line; decoding one against the other's table produces a confidently
  wrong label. Observed: `3000` on an accepted new order, `3002` on an accepted
  modify or cancel, `3003` asking for confirmation.
- The quantity field is documented as `richiesta|eseguita`, a pair when part of
  the order fills immediately — so not always a plain number.
- The trailing field is documented as `<DESCRIZIONE ERRORE>`, but a `TRADOK` for
  an order accepted and not yet executed carries `0.0`, which reads as an
  execution price. The client reports a numeric value as `executed_price` and
  anything else as `message`.

### Only submission needs confirming

`MODORD` and `REVORD` answer `TRADOK;…;3002` directly — no `TRADCONFIRM`, no
second step. The two-phase dance applies to submitting a new order, not to
changing or cancelling one.

### A modify is a cancel-and-replace that reuses the id

After `MODORD`, `ORDERLIST` holds **two rows carrying the same order id**: the
previous price marked `2004` (revoked) and the new price `2000` (working).

```
ORDER;<ticker>;<ora>;MCP1700000000;ACQAZ;30.0;0.0;35;2004
ORDER;<ticker>;<ora>;MCP1700000000;ACQAZ;29.5;0.0;35;2000
```

So **`order_id` is not unique in `ORDERLIST`** — there is one row per state
transition, and filtering by id can return several. Take the one whose status is
live, or use `ORDERLISTPENDING`. This also explains order lists that look
duplicated: an id appearing twice at different prices is one order that was
modified, not two.

### Where a working order shows up, and where it does not

A resting buy does **not** reduce the liquidity that `INFOACCOUNT` or
`INFOAVAILABILITY` report: with a four-figure buy working, both figures were
unchanged to the cent. Committed funds are invisible there, so liquidity alone
overstates what is actually free.

The position does show it. `INFOSTOCKS` reported `quantity_trading` of `35` for
a pending buy and `-20000` for a working sell on another instrument — the
**sign gives the direction**, and this field is where exposure committed to open
orders is visible. Cancelling returned it to `0`.

## There is no simulated mode

Worth stating plainly, because it shapes how this can be tested. The dAPI has
**no** simulation or paper-trading command: nothing in the protocol accepts an
order without sending it to market. And Directa's documentation is explicit that
no test account exists for the purpose:

> Per questa ragione Directa non fornisce alcun *conto prova* per sviluppare
> applicazioni esterne.

The free 15-day demo account with €100,000 of virtual capital covers Directa's
*platforms*, not dAPI development. The only test environment referenced anywhere
is Darwin Command Line's `-test` flag (`java -jar DCL.jar <user> <pass> -test`),
which launches Darwin against a test backend; whether the dAPI sockets are
usable there is unverified.

So any library offering a "simulation mode" for this API is fabricating its
responses client-side, and an order path can only be verified against a real
account.
