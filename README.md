# Turtle Stance 🐢

A Binance USDⓈ-M **smart-money copy bot** with a localhost dashboard. It mirrors a
chosen Binance smart-money trader's futures positions **proportionally** into your own
account, sizing each move to **your** balance.

> ⚠️ **Disclaimer:** For education/research. Futures trading is high risk — you can lose
> all your capital. Test on **testnet** first (`USE_TESTNET = True`). Use at your own risk.
> No personal keys, tokens or account identifiers are included in this repository; you
> supply your own in `config.py` and the extension (both gitignored / placeholder).

---

## Core idea

The bot only acts when the **trader changes their position size**, and applies that change
scaled to **your current balance**:

| Trader does | Bot does |
|---|---|
| Opens a position | Opens, scaled to your balance |
| Increases size | Adds the delta, scaled to current balance |
| Reduces size | Reduces proportionally |
| Closes | Closes |
| **Nothing** | **Nothing** |

Your balance changes (deposits/withdrawals/PnL) **never** move open positions on their own —
only the trader's actual size changes do. See [AUDIT.md](AUDIT.md) for the full risk model.

Scale: `scale = your_wallet_balance / trader_balance`, target delta = `trader_delta × scale × SIZE_MULTIPLIER`.

---

## How data flows

A small **browser extension** reads the trader's public smart-money positions from your
already-logged-in Chrome session and POSTs them to the local bot
(`http://127.0.0.1:8777/ingest`). No separate Chrome, re-login or CDP needed.

```
Binance (extension) → /ingest (token-guarded) → bot loop → tracker + engine → Binance order
```

---

## Files

```
app.py                # Flask dashboard + bot loop (single process, entry point)
copy_bot.py           # Binance executor wrapper + market data + CLI
engine/
  sync_engine.py      # Pure logic: plan_orders (target vs current -> orders), round_step
  tracker.py          # CopyTracker: per-instance baseline/applied/tref, follow/copy, persistence
extension/            # Chrome extension data bridge (Load unpacked)
tests/                # Pure-logic unit tests (engine + tracker)
config.example.py     # Copy to config.py and fill in (config.py is gitignored)
AUDIT.md              # Security & risk audit
```

---

## Setup

1. **Python deps:** `pip install -r requirements.txt`
2. **Config:** `cp config.example.py config.py` and fill in your Binance API key/secret,
   the trader profile id (`TOP_TRADER_ID`), and a random `API_TOKEN`.
   - API key: enable **futures**, disable withdrawals, restrict to your IP.
3. **Extension:** open `extension/background.js` and `extension/inject.js`, replace the
   `PASTE_…` placeholders with your trader profile id, and set the extension `TOKEN`
   (in `background.js`) to the **same** value as `config.py`'s `API_TOKEN`.
4. **Load the extension:** Chrome → `chrome://extensions` → enable Developer mode →
   **Load unpacked** → select `extension/`. Keep a logged-in `binance.com` tab open.
5. **Run:** `python3 app.py` → open `http://127.0.0.1:8777`.

Start on **testnet** (`USE_TESTNET = True`, `DRY_RUN = True`) until you trust the behavior.

---

## Dashboard

- **Follow:** does *not* open the current position; baselines it now and copies only
  **future** changes (increase/decrease/close).
- **Copy:** opens the trader's current position immediately (scaled), then tracks deltas.
- **🛑 Close All:** market-closes every position the bot manages and drops all follows.

---

## Tests

```
python3 -m unittest discover -s tests -q
```

Pure-logic tests for the sync engine and tracker (no network).

---

## License

MIT. See `AUDIT.md` before running with real funds.
