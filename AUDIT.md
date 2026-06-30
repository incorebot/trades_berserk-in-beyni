# Turtle Stance — Security & Risk Audit 🐢

> Live-funds futures bot. Read this fully before running with real money. This document
> is the honest risk model: what is handled, what is **not**, and the worst cases.

---

## 1. Trust & data flow

```
Binance (your logged-in Chrome) ── extension ──> POST /ingest (token) ──> bot loop ──> Binance orders
```

- The bot itself never logs into Binance; it executes orders via your API key
  (`python-binance`, USDⓈ-M, **always MARKET**).
- Trader data comes from a browser extension reading the **public** smart-money profile
  from your own session, then POSTing to `http://127.0.0.1:8777/ingest`.

### Secrets (never commit)
| Secret | Where it lives | Protection |
|---|---|---|
| Binance API key/secret | `config.py` | `.gitignore` |
| `API_TOKEN` (localhost guard) | `config.py` **and** extension `background.js` | placeholder in repo; you fill both with the same value |
| Telegram token / chat id | `config.py` | `.gitignore` |
| Trader profile id | `config.py` / extension placeholders | not a secret, but kept out of the repo |

The repository ships **placeholders only** — no real keys, tokens, IPs or account ids.

---

## 2. What is handled (design guarantees)

- **Delta model:** the bot acts **only when the trader changes position size**. Your own
  balance changes (deposits/withdrawals) and PnL/mark moves do **not** move open positions.
  Each trader change is scaled to your **current** balance.
  - Increase → add `delta × scale × SIZE_MULTIPLIER`
  - Decrease → reduce proportionally · Close → close
- **Scale uses wallet balance**, not margin balance, so unrealized PnL does not cause
  whipsaw (no tick-by-tick re-sizing).
- **Per-instance tracking:** a close+reopen of the same symbol is a new instance; old
  follow state is not carried over. State persisted to `.follow_state.json`.
- **Orphan close:** when the trader fully closes a symbol, the bot closes our share with a
  `reduceOnly` MARKET order (and clears the bookkeeping if the position is already flat).
- **API_TOKEN guard:** `/ingest` and all `/api/*` POST endpoints require the token; no CORS.
- **Stale-data guard:** no orders if the last ingest is older than 90s.
- **Cross-margin set** before exposure-increasing orders; reversal protection (close before
  flipping); rate-limit backoff on Binance `-1003`.
- **Close All (panic):** market-closes every managed position and drops all follows.
- **DRY_RUN / testnet:** no live orders until you explicitly switch them off.

---

## 3. Residual risks (NOT fully mitigated)

| # | Risk | Mechanism | Worst case |
|---|---|---|---|
| R1 | **Missed trader exit on data gap** | If the extension/bot is down, a trader close is never seen. Stale-guard stops *new* orders but does **not** close what you hold. | Trader exited; you stay in a leveraged position as price runs against you. |
| R2 | **`reduceOnly=False` on increases** | Increase orders don't use reduceOnly (to net with manual positions). Reduce/close paths **do**. | If bookkeeping (`applied`) drifts from the real position (partial fills, manual edits), an increase can over-shoot. |
| R3 | **Leverage is copied from the trader** | `ensure_leverage(trader_leverage)`; failures are logged, not fatal. | A 50x copy liquidates on a ~2% adverse move; a failed leverage change leaves a wrong liq distance. |
| R4 | **Cross-margin contagion** | All symbols share one wallet; per-symbol targets, no total-margin cap. | One liquidation can cascade across the whole account. |
| R5 | **MARKET slippage on thin alts** | Every order is MARKET. | Large orders on illiquid symbols fill at bad prices. |
| R6 | **No auto kill-switch** | `ALERT_LOSS_USDT` only **alerts**. | Unattended drawdown is not auto-flattened. |
| R7 | **One-way mode assumed** | Orders carry no `positionSide`. | On a hedge-mode account, orders may be rejected or misrouted. |
| R8 | **Dynamic IP** | Binance API key is IP-restricted. | A new IP (modem/PC restart) breaks the API with `-2015` until you re-whitelist. |
| R9 | **Single-instance only by port** | Protection relies on the `8777` port clash. | Two processes could double-send. |

---

## 4. Recommended guardrails (not enabled by default)

1. **`MAX_LEVERAGE` cap** — never exceed N× regardless of the trader (mitigates R3).
2. **Auto kill-switch** — flatten everything when managed PnL crosses a loss threshold (R6).
3. **Total-notional / effective-leverage cap** — a new entry cannot exceed X× wallet (R3/R4).
4. **Data-gap policy** — optional auto-flatten after N minutes without data (R1).
5. **Real-position verification** — base reduce math on Binance's actual size, not `applied` (R2).

---

## 5. Operational checklist

- [ ] `config.py` filled, **not** committed (verify `git status`).
- [ ] API key: futures enabled, **withdrawals disabled**, **IP-restricted**.
- [ ] Extension `TOKEN` == `config.py` `API_TOKEN`.
- [ ] Started on **testnet** + `DRY_RUN=True`; watched real behavior before going live.
- [ ] `SIZE_MULTIPLIER` and `MANUAL_BALANCE` understood — they directly scale risk.
- [ ] You know where **Close All** is.

> Trading futures can lose all your capital. This software is provided as-is, without
> warranty. You are solely responsible for any orders it places.
