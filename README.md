# directa-mcp

MCP server exposing Directa SIM's Darwin API (dAPI) to any MCP client — positions, balance, orders, historical data.

## Local, not cloud

Darwin opens TCP sockets on `127.0.0.1` (10002 trading, 10003 historical) only while the app is running and logged in. So this is a **local** stdio server, launched by your client on the same machine as Darwin — not a remote connector.

That rules out hosts which only accept remote servers over HTTP: ChatGPT on the web and desktop is one, so use Codex CLI on that side. Anything that can start a local process works.

There are no API keys to configure: the authentication is Darwin being logged in on your machine.

## Prerequisites

1. An active Directa account.
2. Darwin installed (needs a Java JRE/JDK) and logged in.
3. API access enabled: sign the disclaimer in the reserved area on directatrading.com, then check **Sviluppatori > Dev kit** in Darwin.
4. Historical tools (candles, ticks) additionally need the real-time quote entitlement. Without it every historical command answers `1032 — datafeed non abilitato`; `get_darwin_status` reports this up front as `datafeed_enabled`.

## Install

Every client is asking the same thing of you — the command that starts the server. `uvx` fetches the package, provisions a suitable Python and runs it in an isolated environment, so there is nothing to clone and no virtualenv to manage; only [uv](https://docs.astral.sh/uv/) is needed. Pick a tag from the [releases](https://github.com/simoneb/directa-mcp/releases), ideally the latest, and substitute it for `<TAG>` throughout.

```
uvx --from "https://github.com/simoneb/directa-mcp/archive/refs/tags/<TAG>.tar.gz" directa-mcp
```

Most clients take that as JSON, and the same block works across them:

```json
{
  "mcpServers": {
    "directa": {
      "command": "uvx",
      "args": [
        "--from",
        "https://github.com/simoneb/directa-mcp/archive/refs/tags/<TAG>.tar.gz",
        "directa-mcp"
      ],
      "env": {
        "DIRECTA_ENABLE_ORDERS": "false"
      }
    }
  }
}
```

What changes is where that block goes:

| Client | File |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json`, or `~/Library/Application Support/Claude/` on macOS |
| Cursor | `~/.cursor/mcp.json`, or `.cursor/mcp.json` for one project |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Gemini CLI | `~/.gemini/settings.json`, or `.gemini/settings.json` for one project |

Two clients want a different shape:

- **VS Code** keys it under `servers` rather than `mcpServers` and expects `"type": "stdio"` next to `command`, in `.vscode/mcp.json` or the profile file that **MCP: Open User Configuration** opens.
- **Codex CLI** takes TOML in `~/.codex/config.toml`:

  ```toml
  [mcp_servers.directa]
  command = "uvx"
  args = ["--from", "https://github.com/simoneb/directa-mcp/archive/refs/tags/<TAG>.tar.gz", "directa-mcp"]

  [mcp_servers.directa.env]
  DIRECTA_ENABLE_ORDERS = "false"
  ```

Several clients will write the entry themselves, which spares you the shape entirely — `claude mcp add`, `codex mcp add`, `code --add-mcp`, or a guided **Add Server** command. For instance:

```
claude mcp add directa --scope user -- uvx --from "https://github.com/simoneb/directa-mcp/archive/refs/tags/<TAG>.tar.gz" directa-mcp
```

Restart the client afterwards if it does not pick the server up on its own.

Pin a tag rather than a branch: without one every start would pull the tip of `master`, which is not what you want from a tool that talks to your account. To upgrade, change the tag.

Prefer the archive URL over `git+https://…`. The git form makes `uvx` shell out to `git`, and a client that hands the server a minimal environment can leave it unable to find one: Claude Desktop passes no `PATHEXT`, so the lookup fails with `Git executable not found` even where git is installed and on `PATH`. The archive needs neither git nor credentials.

**Working from source** (to develop the server, not to use it):

```powershell
uv venv
uv pip install -e ".[dev]"
pytest
```

Tests run against a fake Darwin over sockets, so the platform need not be running. Register the venv's Python by absolute path, since the client starts the process with its own environment and no venv active — `<repo>\.venv\Scripts\python.exe` with arguments `-m directa_mcp.server`, in place of the `uvx` command above.

## Usage

The tools describe themselves, so ask in plain language: how the portfolio is doing, which positions are down, whether there are open orders, how much is available to invest, whether Darwin is connected.

Current prices for arbitrary symbols, and charts, do not work without the datafeed entitlement. Prices for **your** positions do — those are derived from the portfolio.

Don't ask the model to compute values from `get_positions`: `quantity × price` is wrong by 100× on bonds. `get_portfolio_overview` handles the convention and reconciles its own arithmetic against Darwin's figures; when `reconciliation.reconciled` is false the totals are not to be presented as fact.

## The order gate

Tools that place, modify or cancel orders work **only** when the server runs with `DIRECTA_ENABLE_ORDERS=true`. Otherwise they send nothing at all to Darwin and return `success: false` with `blocked: true`.

**There is no simulated mode**, and that is not this project's choice: the dAPI has no command that accepts an order without sending it to market, and Directa states plainly that it provides no test account for developing external applications. So any library offering a "simulation mode" for this API is inventing the responses client-side — which is exactly what the previous version of this server did, returning a plausible acknowledgement for an order that never left.

The flag is therefore a safety catch, not a mode selector. It lives in the MCP process configuration, so the model cannot turn it on — only you can, by editing the config and restarting. Which also means you can register the server for portfolio questions and leave the whole trading surface inert.

With the flag on, orders are real, with real money.

`cancel_all_orders` is the one operation that can reach an order this server did not create, so its symbol filter was verified deliberately rather than assumed: two working orders on different symbols, `cancel_all_orders` on one, and the other still on the book afterwards.

## Starting Darwin

With `DIRECTA_AUTOSTART=true` the server exposes `start_darwin`, which launches **dGO**, Directa's launcher. It cannot launch Darwin directly: Darwin has no installed executable, only a jar that dGO downloads and starts with a session token reissued at every login, so that command line is not repeatable.

For it to reach Darwin instead of stopping at dGO's tile grid, set **AutoSelezione** to "Darwin 2" in dGO's Preferenze. The server does not write that setting, or anything else on the machine — no files, no registry keys, no process it did not start itself.

**A human stays in the path**: Darwin asks for an OTP. So the tool does not wait — it returns as soon as dGO is up, and you complete the login. From there `check_connection` reports one of three states:

| `darwin.state` | meaning |
|---|---|
| `running` | the ports answer, nothing to start |
| `stopped` | no ports, and no Directa process on the machine |
| `starting` | ports closed, but either dGO was launched from here recently or Directa software is running — typically waiting on the OTP |

`start_darwin` launches **only** in `stopped`. Directa allows one session at a time, and a second dGO risks dropping the login in progress. It never closes Darwin, and there is no `stop_darwin`: shutting down a platform that may have working orders on the book is not a decision to automate.

The flag is separate from `DIRECTA_ENABLE_ORDERS` on purpose — starting the platform and being allowed to send it orders are two distinct consents.

Note that the ports open a few seconds before the connection is established: in that window `get_darwin_status` answers `CONN_UNAVAILABLE` with the ports already reachable. That is startup in progress, not a fault.

Because the OTP is mandatory, this server **cannot be automated unattended** — cron jobs, scheduled agents. Someone has to type the code.

## Tools

| Tool | Description | Status |
|---|---|---|
| `check_connection` | TCP reachability of the Darwin ports, plus `running`/`starting`/`stopped` (needs no login) | ✅ |
| `start_darwin` | Launches dGO so you can start Darwin; does not wait, the OTP stays yours. Needs `DIRECTA_AUTOSTART=true` | ✅ |
| `get_darwin_status` | Connection state, release, whether the datafeed is enabled | ✅ |
| `get_account_balance` | Liquidity, open P&L, equity (`INFOACCOUNT`) | ✅ |
| `get_availability` | Buying power, with and without margin (`INFOAVAILABILITY`) | ✅ |
| `get_positions` | Every open position, as Darwin reports it | ✅ |
| `get_position` | A single position by symbol | ✅ |
| `get_portfolio_overview` | Positions with derived price and value, totals and P&L, with a reconciliation check | ✅ |
| `get_orders` | The day's orders with decoded state; `pending_only` for the live ones | ✅ |
| `preview_limit_order` | What an order would cost — commission, amount, instrument — **without placing it** | ✅ |
| `place_limit_order` | Buy/sell limit order, confirmed within the same connection | ✅ |
| `modify_order` | Change the price of a working order | ✅ |
| `cancel_order` | Cancel one order by id | ✅ |
| `cancel_all_orders` | Cancel every order on a symbol | ✅ |
| `get_daily_candles` | Daily OHLC candles | ⚠️ needs datafeed |
| `get_intraday_candles` | Intraday candles, configurable period | ⚠️ needs datafeed |
| `get_candle_data_range` | Candles over an explicit date range | ⚠️ needs datafeed |
| `get_tick_data` | Tick-by-tick data | ⚠️ needs datafeed |

Three symbol formats coexist: `ENI.MI` for Borsa Italiana stocks, bare tickers for ETFs (`VWCE`, `IWDA`), `M.<number>` for bonds. Read the format back from `get_positions` rather than guessing at it.

Each tool carries the protocol's annotations — `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` — so a client can sort them into permission groups instead of one undifferentiated list, and ask for approval only where it matters. The twelve reading tools are marked read-only; the six that put a command on the wire are not. `preview_limit_order` is the one worth knowing about: it places nothing, but it does send `ACQAZ`, so it is annotated as a write rather than flattered into the read-only group.

## Two data traps

**`INFOACCOUNT` field names do not describe their contents.** Measured against the portfolio, `gain_euro` is the open P&L, while `open_profit_loss` is the portfolio's **carrying cost**, reconciled to the cent. Reading it as a gain overstates it by nearly an order of magnitude. The names stay Directa's rather than invented ones, and every response carries its `raw` line.

**Bonds are quoted as a percentage of nominal.** In `STOCK;M.100001;10:00:00;20000;0;0;95.0;200` the quantity is 20,000 of nominal and the price is 95.00% of it: the position is worth **€19,000**, not €1,900,000. The naive calculation is wrong by 100×.

## Implementation

The dAPI is line-oriented text and the client lives in one file, [`src/directa_mcp/dapi.py`](src/directa_mcp/dapi.py), with no dependencies beyond `mcp` and `python-dotenv`. Every command and response format it uses is documented in [`docs/PROTOCOL.md`](docs/PROTOCOL.md), together with the raw transcripts it was derived from.

Framing is deterministic via `FLOWPOINT TRUE`, which wraps lists in `BEGIN`/`END` markers, so a response is read up to its terminator instead of waiting for the socket to fall quiet; replies are selected by prefix and everything else is routed to `unsolicited`. If Darwin refuses `FLOWPOINT`, the client refuses to start rather than guess where a portfolio ends.

The project began on the community library [`directa-api-python`](https://github.com/NiccoloSalvini/directa-api-python), dropped after testing against live Darwin — see [`docs/UPSTREAM.md`](docs/UPSTREAM.md) for the three defects and the fixes proposed upstream.
