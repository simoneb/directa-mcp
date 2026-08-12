# directa-mcp

Server MCP che espone la Darwin API (dAPI) di Directa SIM come tool per Claude — posizioni, saldo, ordini, dati storici.

## Come funziona

L'API di Directa **non è cloud**: Darwin apre dei socket TCP in ascolto su `127.0.0.1` (trading sulla 10002, dati storici sulla 10003) solo mentre l'app è avviata e loggata. Di conseguenza questo è un server MCP **locale** (stdio), lanciato da Claude Desktop o Claude Code sulla stessa macchina dove gira Darwin — non un connector remoto da aggiungere in claude.ai/settings come quello di Interactive Brokers.

Il protocollo dAPI è testuale a righe e il client è implementato qui, in [`src/directa_mcp/dapi.py`](src/directa_mcp/dapi.py), senza dipendenze esterne oltre a `mcp` e `python-dotenv`. Il progetto era partito appoggiandosi alla libreria community [`directa-api-python`](https://github.com/NiccoloSalvini/directa-api-python), rimossa dopo i test contro Darwin reale — vedi [Perché un client interno](#perché-un-client-interno).

Ogni comando e formato di risposta usato dal client è documentato in [`docs/PROTOCOL.md`](docs/PROTOCOL.md) con le trascrizioni raw da cui è stato ricavato.

## Prerequisiti

1. Conto Directa attivo.
2. Darwin installato (richiede Java JRE/JDK) e avviato, loggato con l'account.
3. Accesso API abilitato: firma il disclaimer nell'area riservata su directatrading.com, poi in Darwin vai su **Sviluppatori > Dev kit** per verificare che i socket siano attivi.
4. Per i dati storici (candele, tick) serve **anche** l'abilitazione alle quotazioni real-time sul conto. Senza, ogni comando storico risponde `1032 — datafeed non abilitato`; lo vedi in anticipo dal campo `datafeed_enabled` di `get_darwin_status`.

## Setup

```powershell
cd D:\dev\trading\directa-mcp
uv venv
uv pip install -e ".[dev]"
copy .env.example .env
pytest
```

(o con `pip`: `python -m venv .venv`, poi `.venv\Scripts\pip install -e ".[dev]"`)

I test girano contro un finto Darwin su socket ([`tests/fake_darwin.py`](tests/fake_darwin.py)) e non richiedono la piattaforma avviata.

## Come si usa

### 1. Registra il server

Non serve clonare il repository né gestire un virtualenv: `uvx` scarica il pacchetto, si procura un Python adeguato e lancia l'entry point in un ambiente isolato. Serve solo [uv](https://docs.astral.sh/uv/) installato.

**Claude Code**, disponibile da qualsiasi directory:

```powershell
claude mcp add directa --scope user -- uvx --from "git+https://github.com/simoneb/directa-mcp@v0.1.0" directa-mcp
```

**Claude Desktop** — aggiungi a `%APPDATA%\Claude\claude_desktop_config.json` e riavvia l'app:

```json
{
  "mcpServers": {
    "directa": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/simoneb/directa-mcp@v0.1.0",
        "directa-mcp"
      ],
      "env": {
        "DIRECTA_ENABLE_ORDERS": "false"
      }
    }
  }
}
```

Il riferimento `@v0.1.0` è un tag, non un branch: pinna una versione precisa. Senza, ogni avvio prenderebbe l'ultimo commit di `master`, che su uno strumento che parla col tuo conto non è desiderabile. Per aggiornare, cambia il tag.

**Finché il repository è privato** questo funziona solo su una macchina il cui `git` è già autenticato verso GitHub — per esempio via Git Credential Manager, o dopo `gh auth setup-git`. Verificato: `uvx` delega a `git`, che usa il credential helper, quindi non serve nessun token nel file di configurazione. Su una macchina non autenticata la risoluzione fallisce con un errore di accesso al repository.

**Se preferisci lavorare dai sorgenti** (per sviluppare il server, non per usarlo) la strada resta quella del [Setup](#setup), e la registrazione punta al Python del venv con percorso assoluto — Claude Desktop avvia il processo con un ambiente proprio, senza il venv attivo:

```powershell
claude mcp add directa-dev --scope user -- D:\dev\trading\directa-mcp\.venv\Scripts\python.exe -m directa_mcp.server
```

### 2. Avvia Darwin

Il server parla con Darwin, non con Directa. Se Darwin è chiuso o disconnesso ogni tool fallisce, e `check_connection` te lo dice in chiaro. Non serve nessuna chiave API né credenziale nella configurazione: l'autenticazione è il fatto che Darwin è loggato sulla tua macchina.

### 3. Chiedi in italiano

Non c'è niente da imparare a memoria — i tool si descrivono da soli e Claude sceglie quale usare. Cose che funzionano oggi:

- *"Come sta andando il portafoglio?"* → `get_portfolio_overview`: ogni posizione con prezzo corrente, valore, P&L, e i totali
- *"Quanto ho investito in obbligazioni rispetto agli ETF?"*
- *"Quali sono le mie posizioni in perdita?"*
- *"Ho ordini aperti?"* → `get_orders` con `pending_only`
- *"Quanto posso investire?"* → `get_availability`
- *"Com'è andato l'ordine su M.100001?"*
- *"Darwin è connesso?"* → `check_connection`

Cose che **non** funzionano, e perché:

- *"Qual è il prezzo di ENI adesso?"* — niente quotazioni: il datafeed real-time non è abilitato sul conto. I prezzi delle **tue** posizioni sì, quelli si ricavano dal portafoglio.
- *"Fammi un grafico dell'ultimo mese"* — candele e tick richiedono la stessa abilitazione.
- *"Compra 100 ENI"* — bloccato, salvo che tu abbia armato il server: vedi sotto.

### Una nota sull'affidabilità dei numeri

Darwin non espone un prezzo corrente su questa porta. `get_portfolio_overview` lo ricava dal prezzo medio e dal gain teorico, gestisce la convenzione percentuale delle obbligazioni, e poi **verifica i propri conti** contro le cifre di Darwin. Se la verifica non torna, il campo `reconciliation.reconciled` è `false` e Claude te lo deve dire invece di presentare i totali come fatti. Sul conto testato il residuo è 0,00 €.

Non chiedere a Claude di calcolarsi i valori da `get_positions`: `quantità × prezzo` su un'obbligazione sbaglia di 100 volte.

## Cosa è verificato e cosa no

Testato contro **Darwin 2.5.1** su un conto reale (`PROD`), il 2026-08-12:

- ✅ `check_connection`, `get_darwin_status`
- ✅ `get_account_balance`, `get_availability`
- ✅ `get_positions` (15 posizioni, lista completa), `get_position`
- ✅ `get_portfolio_overview`, con riconciliazione a **0,00 €** di residuo contro le cifre di Darwin su 15 posizioni
- ✅ `get_orders`, incluso `pending_only`
- ✅ Il gate sugli ordini: con `DIRECTA_ENABLE_ORDERS` non impostato i tool di trading non inviano nulla
- ⚠️ Tool storici (`get_daily_candles`, `get_intraday_candles`, `get_candle_data_range`, `get_tick_data`): raggiungono Darwin e restituiscono correttamente l'errore, ma **non è stato possibile vedere dati reali** perché il conto di test non ha le quotazioni abilitate (`1032`). Il formato delle righe `CANDLE;`/`TBT;` viene dalla documentazione ed è da confermare.
- ❌ Tool sugli ordini (`place_limit_order`, `modify_order`, `confirm_order`, `cancel_order`, `cancel_all_orders`): **non verificati**. L'unico conto disponibile è reale, con posizioni e ordini vivi, e non è stato inviato nessun ordine. La sintassi dei comandi viene dalla documentazione, corroborata dal campo `operation` degli `ORDER` piazzati dall'interfaccia di Darwin. Al primo uso reale, verifica l'esito con `get_orders` invece di fidarti dell'ack.

## Perché un client interno

Testando il server contro Darwin reale, la libreria community si è rivelata inservibile sul percorso di lettura, con tre problemi:

1. `get_portfolio()` invia `GETPORTFOLIO` e `get_account_info()` invia `GETACCTINFO`. Darwin rifiuta entrambi con `ERR 1004`: i comandi giusti sono `INFOSTOCKS` e `INFOACCOUNT`. Curiosamente i parser della libreria sono già scritti per quei due comandi — è solo l'invio che è sbagliato.
2. Per le risposte multi-riga, `send_command` restituisce **solo la prima riga** che corrisponde al prefisso atteso. Osservato dal vivo: `ORDERLIST` restituisce 4 ordini sul socket, `get_orders()` ne restituiva 1. Con il comando portafoglio corretto avrebbe perso 14 posizioni su 15 — perdita di dati silenziosa, senza errore.
3. La lettura non isola la risposta dal traffico non richiesto. Darwin pusha l'intero portafoglio e la lista ordini a ogni connessione, e poi aggiornamenti spontanei: senza filtro, prima o poi si legge la posta di qualcun altro.

Il client interno risolve questi punti alla radice: comandi verificati uno per uno, framing **deterministico** via `FLOWPOINT TRUE` (che fa avvolgere le liste in marker `BEGIN`/`END`, quindi si legge fino al terminatore invece di aspettare che il socket taccia), e risposte selezionate per prefisso con tutto il resto instradato in `unsolicited`. Se Darwin rifiuta `FLOWPOINT`, il client si rifiuta di partire anziché tirare a indovinare dove finisce un portafoglio.

Gli stessi fix sono stati proposti upstream alla libreria — vedi [`docs/UPSTREAM.md`](docs/UPSTREAM.md).

## Sicurezza — il gate sugli ordini

I tool che immettono, modificano o cancellano ordini funzionano **solo** se il server è avviato con `DIRECTA_ENABLE_ORDERS=true`. Altrimenti non inviano nulla a Darwin e restituiscono `success: false` con `blocked: true`.

**Non esiste una modalità simulata**, e non è una scelta di questo progetto: la dAPI non ha alcun comando che accetti un ordine senza mandarlo a mercato, e la documentazione Directa è esplicita — *"Directa non fornisce alcun conto prova per sviluppare applicazioni esterne"*. La demo 15 giorni a 100.000 € virtuali riguarda le piattaforme, non lo sviluppo su API. Quindi qualunque libreria che offra una "simulation mode" per questa API sta inventando le risposte lato client: è esattamente ciò che faceva la versione precedente di questo server, restituendo per un ordine mai partito un ack plausibile e indistinguibile da uno vero.

Di conseguenza il flag non è un selettore di modalità ma una **sicura**. Vale per tre motivi:

1. Il server è pilotato da un modello. Senza il flag, un'istruzione fraintesa non può raggiungere Darwin.
2. Vive nella configurazione del processo MCP, quindi il modello non può accenderlo: solo tu, modificando la config e riavviando.
3. Puoi registrare il server per le domande sul portafoglio lasciando inerte tutta la superficie di trading.

Si chiamava `DIRECTA_LIVE_TRADING`, nome che implicava l'esistenza di un trading non-live. Rinominato per dire cosa fa davvero.

Con il flag a `true` gli ordini sono reali, con soldi reali, e sono la parte non verificata di questo server.

## Tool esposti

| Tool | Descrizione | Stato |
|---|---|---|
| `check_connection` | Raggiungibilità TCP delle porte Darwin (diagnostica, non richiede login) | ✅ |
| `get_darwin_status` | Stato connessione, release, e se il datafeed è abilitato | ✅ |
| `get_account_balance` | Liquidità, P&L aperto, equity (`INFOACCOUNT`) | ✅ |
| `get_availability` | Potere d'acquisto, con e senza margine (`INFOAVAILABILITY`) | ✅ |
| `get_positions` | Tutte le posizioni aperte, come le riporta Darwin | ✅ |
| `get_portfolio_overview` | Posizioni con prezzo e valore ricavati, totali e P&L, con verifica di riconciliazione | ✅ |
| `get_position` | Una singola posizione per simbolo | ✅ |
| `get_orders` | Ordini della giornata con stato decodificato; `pending_only` per i soli attivi | ✅ |
| `place_limit_order` | Ordine limite buy/sell | ❌ non verificato |
| `modify_order` | Modifica prezzo di un ordine aperto | ❌ non verificato |
| `confirm_order` | Conferma un ordine che ha risposto `TRADCONFIRM` | ❌ non verificato |
| `cancel_order` | Cancella un ordine per ID | ❌ non verificato |
| `cancel_all_orders` | Cancella tutti gli ordini su un simbolo | ❌ non verificato |
| `get_daily_candles` | Candele giornaliere OHLC | ⚠️ serve datafeed |
| `get_intraday_candles` | Candele intraday con periodo configurabile | ⚠️ serve datafeed |
| `get_candle_data_range` | Candele su un range di date esplicito | ⚠️ serve datafeed |
| `get_tick_data` | Dati tick-by-tick | ⚠️ serve datafeed |

Tre formati di simbolo convivono: `ENI.MI` per i titoli di Borsa Italiana, ticker nudo per gli ETF (`VWCE`, `IWDA`), `M.<numero>` per le obbligazioni. Il modo affidabile per sapere quale usare è leggerlo da `get_positions`.

## Due trappole nei dati, documentate

**I nomi dei campi di `INFOACCOUNT` non descrivono il loro contenuto.** Misurato contro il portafoglio: `gain_euro` è il P&L aperto (coincide con la somma dei `theoretical_gain`), e `open_profit_loss` è il **costo di carico** del portafoglio, riconciliato al centesimo su 15 posizioni. Quindi `open_profit_loss` contiene un costo, non un profitto: leggerlo come guadagno lo sovrastima di quasi un ordine di grandezza. I nomi restano quelli di Directa invece di inventarne altri, e ogni risposta porta la riga `raw`.

**Le obbligazioni sono quotate in percentuale del nominale.** In `STOCK;M.100001;10:00:00;20000;0;0;95.0;200` la quantità è 20.000 di nominale e il prezzo è il 95,00% di esso: la posizione vale **19.000 €**, non 1.900.000 €. `get_portfolio_overview` lo gestisce e verifica il risultato contro le cifre di Darwin; il calcolo ingenuo sbaglia di 100 volte.

Dettagli e trascrizioni in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).
