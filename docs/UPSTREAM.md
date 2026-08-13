# Fixes proposed to `directa-api-python`

This project started on [`directa-api-python`](https://github.com/NiccoloSalvini/directa-api-python) (the `directa-api-wrapper` package), the community library mapping the dAPI to Python. Testing against live Darwin showed the read path on the trading port was unusable, so the client was rewritten in-house ([`src/directa_mcp/dapi.py`](../src/directa_mcp/dapi.py)) and the fixes were offered upstream.

**PR: [NiccoloSalvini/directa-api-python#1](https://github.com/NiccoloSalvini/directa-api-python/pull/1)** — *Fix portfolio, account and order reads on the trading port*. Fork: [simoneb/directa-api-python](https://github.com/simoneb/directa-api-python), branch `fix/trading-port-reads`.

## The three defects

1. **Commands Darwin rejects.** `get_portfolio()` sent `GETPORTFOLIO` and `get_account_info()` sent `GETACCTINFO`; Darwin answers `ERR;<command>;1004` to both. The correct commands are `INFOSTOCKS` and `INFOACCOUNT` — already named in the docstrings of the library's own parsers.

2. **Multi-line responses truncated.** `TradingConnection.send_command` returned only the first line matching the expected prefix. Measured: `ORDERLIST` returns four orders on the socket and `get_orders()` returned one; a fifteen-position portfolio arrived as one. Silently, with no error or warning.

3. **No isolation from unsolicited traffic.** Darwin pushes the portfolio and order list on every connection, then spontaneous updates — all of it read as the reply to the command in flight.

The fix reads responses line by line with a persistent buffer, enables `FLOWPOINT` in `connect()` for `BEGIN`/`END` framing, returns every line of a list, exposes the initial push as `pushed_lines`, and no longer fails a command over a link notification (`ERR` codes 1024–1028). It adds twelve tests against a socket-level fake Darwin, runnable in CI without the platform.

## Why an in-house client regardless

- The fixes sit on a `git+https` dependency not published to PyPI, so every user would depend on the merge and on a moving ref.
- The dAPI is line-oriented text and the client fits in one file; the library added surface this server does not need (simulation mode with fabricated data, connection metrics, iterators).
- The safety choices diverge: here the order gate is a flat refusal, whereas the library's `simulation_mode` answers with fabricated data — convenient when developing, risky for a tool driven by a model.

The fork's value is for anyone using the library directly.

## Still unverified upstream

Reported in the PR, unresolved:

- **Order path.** Not exercised there: the only account available is real, with live positions and orders. With the fix, `TRADOK`/`TRADERR`/`TRADCONFIRM` fall through the "unknown prefix" branch of `send_command`; whether that is adequate needs checking.
- **Historical port (10003).** Its `send_command` reads up to `END CANDLES`/`END TBT` and does not share the defect, but real data was never seen: the test account lacks the quote entitlement, so every command answers `1032`.
