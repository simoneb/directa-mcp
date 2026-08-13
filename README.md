# directa-mcp

MCP server exposing Directa SIM's Darwin API (dAPI) to any MCP client — positions, balance, orders, historical data.

Darwin listens on `127.0.0.1` only while it is running and logged in, so this is a **local** stdio server: your client starts it on the same machine. Hosts that only accept remote HTTP servers cannot run it (ChatGPT web and desktop among them; use Codex CLI there). There are no API keys — the authentication is Darwin being logged in.

## Prerequisites

1. An active Directa account, with Darwin installed (needs a Java JRE) and logged in.
2. API access enabled: sign the disclaimer on directatrading.com, then check **Sviluppatori > Dev kit** in Darwin.
3. For candles and ticks only: the real-time data entitlement. Without it those tools fail with `1032`, and `get_darwin_status` reports it as `datafeed_enabled`.

## Install

Pick a tag from the [releases](https://github.com/simoneb/directa-mcp/releases) — the latest unless you have a reason — and substitute it for `<TAG>`. `uvx` fetches and runs the server in an isolated environment; only [uv](https://docs.astral.sh/uv/) is needed.

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

| Client | File |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json`, or `~/Library/Application Support/Claude/` |
| Cursor | `~/.cursor/mcp.json`, or `.cursor/mcp.json` per project |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Gemini CLI | `~/.gemini/settings.json`, or `.gemini/settings.json` per project |

**VS Code** keys it under `servers` and wants `"type": "stdio"`, in `.vscode/mcp.json`. **Codex CLI** takes TOML in `~/.codex/config.toml`:

```toml
[mcp_servers.directa]
command = "uvx"
args = ["--from", "https://github.com/simoneb/directa-mcp/archive/refs/tags/<TAG>.tar.gz", "directa-mcp"]

[mcp_servers.directa.env]
DIRECTA_ENABLE_ORDERS = "false"
```

Or let the client write it: `claude mcp add`, `codex mcp add`, `code --add-mcp`.

Pin a tag, not a branch — otherwise every start pulls the tip of `master`. Prefer the archive URL over `git+https://…`: the git form needs a `git` on PATH, and some clients hand the server too small an environment to find one.

## Tools

| Tool | |
|---|---|
| `check_connection` | Are Darwin's ports up; `running`/`starting`/`stopped`. Needs no login |
| `get_darwin_status` | Connection state, release, whether the datafeed is enabled |
| `get_account_balance` | Liquidity, open P&L, equity |
| `get_availability` | Buying power, with and without margin |
| `get_positions`, `get_position` | Open positions, as Darwin reports them |
| `get_portfolio_overview` | Positions with derived price and value, totals, and a reconciliation check |
| `get_orders` | The day's orders with decoded state; `pending_only` for the live ones |
| `preview_limit_order` | What an order would cost, **without placing it** |
| `place_limit_order`, `modify_order`, `cancel_order`, `cancel_all_orders` | Real orders. Need the gate below |
| `start_darwin` | Launches dGO so you can log in. Needs `DIRECTA_AUTOSTART=true` |
| `get_daily_candles`, `get_intraday_candles`, `get_candle_data_range`, `get_tick_data` | OHLC candles and ticks. Need the datafeed entitlement |

Symbols use Directa's formats: `ENI.MI` for Borsa Italiana stocks, bare tickers for ETFs (`VWCE`), `M.<number>` for bonds. Read one back from `get_positions` rather than guessing.

Each tool carries the protocol's annotations (`readOnlyHint` and friends), so a client can group them by permission and only ask for approval where it matters. The twelve reading tools are marked read-only; the six that put a command on the wire are not — including `preview_limit_order`, which places nothing but does send `ACQAZ`.

## The order gate

Order tools work **only** with `DIRECTA_ENABLE_ORDERS=true`. Otherwise they send nothing and return `success: false` with `blocked: true`.

**There is no simulated mode.** The dAPI has no command that accepts an order without sending it to market, and Directa provides no test account, so any library offering one for this API is fabricating the responses. The flag is a safety catch, not a mode selector: it lives in the process configuration, so the model cannot turn it on — only you can. With it on, orders are real, with real money.

## Starting Darwin

With `DIRECTA_AUTOSTART=true` the server exposes `start_darwin`, which launches **dGO**; Darwin itself has no installed executable. For it to get past dGO's tile grid, set **AutoSelezione** to "Darwin 2" in dGO's Preferenze — the server writes no settings of its own.

Darwin then asks for an OTP, so the tool does not wait: it returns once dGO is up and you finish the login, after which `check_connection` reports `running`. It launches only from `stopped`, since Directa allows one session at a time, and never closes Darwin. For the same OTP reason this server **cannot run unattended**.

## Two data traps

**`INFOACCOUNT` field names do not describe their contents.** `gain_euro` is the open P&L; `open_profit_loss` is the portfolio's **carrying cost**. Reading it as a gain overstates it by nearly an order of magnitude.

**Bonds are quoted as a percentage of nominal.** 20,000 nominal at `95.0` is worth €19,000, not €1,900,000 — the naive `quantity × price` is wrong by 100×. `get_portfolio_overview` handles this and reconciles its own totals against Darwin's; if `reconciliation.reconciled` is false, don't present the numbers as fact.

## Implementation

The client is one dependency-light file, [`src/directa_mcp/dapi.py`](src/directa_mcp/dapi.py). Every command and response format it uses, and the surprises behind them, are in [`docs/PROTOCOL.md`](docs/PROTOCOL.md). The project began on the community library [`directa-api-python`](https://github.com/NiccoloSalvini/directa-api-python), dropped after live testing — see [`docs/UPSTREAM.md`](docs/UPSTREAM.md).
