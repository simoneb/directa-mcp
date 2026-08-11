# directa-mcp

Server MCP che espone la Darwin API (dAPI) di Directa SIM come tool per Claude — posizioni, saldo, ordini, dati storici.

## Come funziona

L'API di Directa **non è cloud**: Darwin apre due socket TCP in ascolto su `127.0.0.1` (trading sulla 10002, dati storici sulla 10003) solo mentre l'app è avviata e loggata. Di conseguenza questo è un server MCP **locale** (stdio), lanciato da Claude Desktop o Claude Code sulla stessa macchina dove gira Darwin — non un connector remoto da aggiungere in claude.ai/settings come quello di Interactive Brokers.

Il client Python verso il socket è la libreria community [`directa-api-python`](https://github.com/NiccoloSalvini/directa-api-python) (non ufficiale, non pubblicata su PyPI — installata da questo progetto come dipendenza git).

## Prerequisiti

1. Conto Directa attivo.
2. Darwin installato (richiede Java JRE/JDK) e avviato, loggato con l'account.
3. Accesso API abilitato: firma il disclaimer nell'area riservata su directatrading.com, poi in Darwin vai su **Sviluppatori > Dev kit** per verificare che i socket siano attivi.

## Setup

```powershell
cd D:\dev\trading\directa-mcp
uv venv
uv pip install -e .
copy .env.example .env
```

(o con `pip`: `python -m venv .venv`, poi `.venv\Scripts\pip install -e .`)

Il pacchetto reale su GitHub si chiama `directa-api-wrapper` (non `directa-api` come suggerirebbe l'URL del repo) — `pip`/`uv` lo installano direttamente da GitHub via l'URL git in `pyproject.toml`, quindi serve Git installato e raggiungibile in PATH.

## Cosa è verificato e cosa no

L'installazione (`uv pip install -e .`) e l'import Python sono stati testati: il pacchetto si installa, `directa_mcp.server` importa senza errori, tutti e 13 i tool si registrano con schema corretto, e `check_connection` gira end-to-end (riporta `reachable: false` perché Darwin non è ancora avviato — comportamento atteso). Le firme dei metodi (`get_account_info`, `get_portfolio`, `buy_limit`, `cancel_order`, `get_daily_candles`, ecc.) sono state confrontate riga per riga col sorgente reale installato sotto `.venv/Lib/site-packages/directa_api/`, non solo col README della libreria.

**Non ancora verificato** — richiede Darwin attivo e loggato: il comportamento reale di connessione/autenticazione, il formato esatto dei dati restituiti da ogni comando (i parser della libreria non sono stati ispezionati in dettaglio), e il piazzamento ordini in simulazione. Quando hai Darwin pronto, fammi girare i tool uno per uno — partendo da `check_connection` e `get_darwin_status` — e sistemiamo insieme quello che non torna.

## Sicurezza — trading live vs simulato

`place_limit_order` invia ordini **reali** solo se il server è avviato con `DIRECTA_LIVE_TRADING=true` nell'ambiente. Di default (`false`, come in `.env.example`) ogni ordine passa comunque per il metodo dell'API ma in **simulation mode** di Darwin — nessun ordine reale parte. Il campo `live` nella risposta del tool dice sempre quale dei due è successo.

Non alzare `DIRECTA_LIVE_TRADING` a `true` finché non hai verificato il comportamento in simulazione.

## Configurazione in Claude Code / Claude Desktop

Aggiungi al tuo file di configurazione MCP (`.mcp.json` di progetto, o la config globale di Claude Desktop):

```json
{
  "mcpServers": {
    "directa": {
      "command": "D:\\dev\\trading\\directa-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "directa_mcp.server"],
      "env": {
        "DIRECTA_LIVE_TRADING": "false"
      }
    }
  }
}
```

## Tool esposti

| Tool | Descrizione |
|---|---|
| `check_connection` | Verifica raggiungibilità TCP delle porte Darwin (diagnostica, non richiede login) |
| `get_darwin_status` | Stato/metriche di connessione via API di trading (più ricco di `check_connection`) |
| `get_account_balance` | Liquidità e saldo conto |
| `get_positions` | Posizioni aperte in portafoglio |
| `get_orders` | Stato ordini della giornata |
| `place_limit_order` | Piazza un ordine limit (buy/sell) — vedi nota sicurezza sopra |
| `modify_order` | Modifica prezzo (e signal price per gli stop) di un ordine aperto |
| `cancel_order` | Cancella un singolo ordine per ID |
| `cancel_all_orders` | Cancella tutti gli ordini aperti su un simbolo |
| `get_daily_candles` | Candele giornaliere OHLC |
| `get_intraday_candles` | Candele intraday con periodo configurabile |
| `get_tick_data` | Dati tick-by-tick |
| `get_candle_data_range` | Candele OHLC su un range di date esplicito |

Simboli nel formato Directa, tipicamente `<TICKER>.MI` per titoli di Borsa Italiana (es. `ENI.MI`).
