# Fix proposti a `directa-api-python`

Questo progetto era partito appoggiandosi a [`directa-api-python`](https://github.com/NiccoloSalvini/directa-api-python) (pacchetto `directa-api-wrapper`), la libreria community che mappa il protocollo dAPI in Python. I test contro Darwin reale hanno mostrato che il percorso di lettura della porta di trading non era utilizzabile, quindi il client è stato riscritto internamente ([`src/directa_mcp/dapi.py`](../src/directa_mcp/dapi.py)) e i fix sono stati proposti upstream.

**PR: [NiccoloSalvini/directa-api-python#1](https://github.com/NiccoloSalvini/directa-api-python/pull/1)** — *Fix portfolio, account and order reads on the trading port*

Fork: [simoneb/directa-api-python](https://github.com/simoneb/directa-api-python), branch `fix/trading-port-reads`.

## I tre problemi

1. **Comandi rifiutati da Darwin.** `get_portfolio()` inviava `GETPORTFOLIO` e `get_account_info()` inviava `GETACCTINFO`; Darwin risponde `ERR;<comando>;1004` a entrambi. I comandi corretti sono `INFOSTOCKS` e `INFOACCOUNT` — già nominati nei docstring dei parser della libreria stessa.

2. **Troncamento delle risposte multi-riga.** `TradingConnection.send_command` restituiva solo la prima riga corrispondente al prefisso atteso. Misurato: `ORDERLIST` restituisce 4 ordini sul socket, `get_orders()` ne restituiva 1; un portafoglio da 15 posizioni arrivava come 1. Senza errore né warning.

3. **Nessun isolamento dal traffico non richiesto.** Darwin pusha portafoglio e ordini a ogni connessione e poi aggiornamenti spontanei; venivano letti come risposta al comando in volo.

Il fix upstream legge la risposta riga per riga con buffer persistente, abilita `FLOWPOINT` in `connect()` per avere il framing `BEGIN`/`END`, restituisce tutte le righe di una lista, espone il push iniziale in `pushed_lines`, e non fa più fallire un comando per una notifica di link (codici `ERR` 1024-1028).

Include 12 test contro un finto Darwin su socket, eseguibili in CI senza la piattaforma.

## Perché comunque un client interno

Anche a PR accettata, questo progetto resta sul client interno:

- I fix sono su una dipendenza `git+https` non pubblicata su PyPI, quindi ogni utente dipenderebbe dal merge e da un ref mobile.
- Il protocollo dAPI è testuale a righe e il client sta in un file: la libreria aggiungeva superficie (simulation mode con dati finti, metriche di connessione, iteratori) di cui questo server non ha bisogno.
- Le scelte di sicurezza divergono: qui il gate sugli ordini è un rifiuto netto, mentre la libreria in `simulation_mode` risponde con dati finti — comodo per lo sviluppo, rischioso per un tool esposto a un modello.

Il valore del fork è per chi usa la libreria direttamente.

## Cosa resta da verificare upstream

Segnalato nella PR, non risolto:

- **Percorso ordini.** Non esercitato: l'unico conto disponibile è reale con posizioni e ordini vivi. Con il fix, le risposte `TRADOK`/`TRADERR`/`TRADCONFIRM` passano dal ramo "prefisso non noto" di `send_command`; va verificato che sia adeguato.
- **Porta storica (10003).** Il suo `send_command` legge fino a `END CANDLES`/`END TBT` e non ha lo stesso difetto, ma non è stato possibile vedere dati reali: il conto di test non ha le quotazioni abilitate e ogni comando risponde `1032`.
