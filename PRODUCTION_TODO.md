# Production Readiness — `execution-engine-multi`

Audit date: 2026-06-15  
Scope: execution-engine-multi (worker + manager + IPC layer)  
Status: **NOT PRODUCTION READY**

Legend: 🔴 Blocker · 🟠 Critical · 🟡 High · 🔵 Medium · ⚪ Low

---

## 🔴 BLOCKERS — system cannot run at all

- [ ] **B1** `manager.app.service.ManagerRuntime` is imported in `__main__.py:86` but the `manager/` package does not exist in this repo — manager mode crashes immediately with `ModuleNotFoundError`
- [ ] **B2** `manager.gui.app.ApexTraderGUI` is imported in `__main__.py:134` — same missing package; GUI entry point is also broken
- [ ] **B3** `ManagerConfig` is imported in `__main__.py:83` but is not defined anywhere in `src/config/settings.py` — manager boot fails before doing anything
- [ ] **B4** `container.signal_consumer.set_execution_event_sink(...)` is called in `__main__.py:53` but `SignalConsumer` has no `set_execution_event_sink` method — worker crashes at startup
- [ ] **B5** `InternalSignalClient` is never started in the worker entry point (`__main__.py`) — the internal signal path from manager → worker is wired to nothing
- [ ] **B6** `WorkerEventClient` and `InternalSignalClient` are not in `AppContainer` — they are invisible to `shutdown()`, tests, and DI tooling
- [ ] **B7** The entire `venv/` directory is committed to the repo — hundreds of megabytes of binaries in git, dependency management via file-copy instead of a lock file, security patches require manual venv replacement

---

## 🟠 CRITICAL SECURITY

### Secrets committed to source control
- [ ] **S1** `execution-gateway/.env` — live `SUPABASE_SERVICE_ROLE_KEY` (full JWT) committed; rotate immediately
- [ ] **S2** `execution-gateway/.env` — `DATABASE_URL` contains plaintext Postgres password `APEX07086262723PROTOCOL`; rotate immediately
- [ ] **S3** `execution-gateway/.env` — `SMTP_PASS` committed; rotate immediately
- [ ] **S4** `execution-gateway/.env` — `PAYSTACK_SECRET_KEY` committed; rotate immediately
- [ ] **S5** `customer-dashboard/.env` — `GATEWAY_ADMIN_KEY` committed; rotate immediately
- [ ] **S6** Add `.env` to `.gitignore` in all sub-packages; add a pre-commit hook that blocks `.env` files from being staged

### IPC transport has no security
- [ ] **S7** Manager-worker IPC uses a raw TCP socket with no TLS — anyone on `127.0.0.1` who connects to `ENGINE_IPC_PORT` can inject arbitrary commands (`worker/event_client.py:104`)
- [ ] **S8** IPC token sent in plaintext in `WORKER_HELLO` payload — interceptable on the loopback interface (`worker/event_client.py:110`)
- [ ] **S9** `ENGINE_IPC_TOKEN` defaults to empty string (`__main__.py:47`) — if env var is missing, worker authenticates with no token
- [ ] **S10** Worker does not verify the manager's identity — any process connecting to the worker's IPC port that knows the token has full command authority

### Signal integrity
- [ ] **S11** `InternalSignalClient._process_signal` emits `SIGNAL_TRIGGERED` directly with no HMAC check, no `SignalValidator`, no deduplication (`signals/internal_client.py:123`) — the entire cryptographic signal-integrity story is absent for the internal path
- [ ] **S12** `SIGNAL_DELIVER` command in `WorkerEventClient._handle_command` calls `InboundSignal.from_dict()` with no validation — the manager can inject any signal into any worker
- [ ] **S13** `signal_hmac_secret` is optional (`signals/consumer.py:67`) — if `SIGNAL_HMAC_SECRET` env var is missing, all gateway-sourced signals are accepted without cryptographic verification
- [ ] **S14** `ws_token` is appended as a URL query parameter (`signals/internal_client.py:42`) — visible in process lists, proxy logs, and server access logs

### Other
- [ ] **S15** `execution-gateway/src/main.ts:17` — CORS defaults to `'*'`; lock to production domain via `GATEWAY_CORS_ORIGIN`
- [ ] **S16** No HTTP security headers (HSTS, X-Frame-Options, X-Content-Type-Options) on the gateway
- [ ] **S17** No replay-attack protection on webhook payloads (no timestamp/nonce check)
- [ ] **S18** `config/settings.py:66` — default broker order comment `"bobisquote"` leaks platform identity; make this configurable or remove
- [ ] **S19** SQLite `engine.db` has no filesystem encryption — filesystem access = full trade history + device credentials

---

## 🟠 CRITICAL — financial correctness

- [ ] **F1** No cross-worker aggregate risk check — two workers can each open positions that together breach the account-level loss limit; `max_open_trades_rule` and `daily_loss_limit_rule` are per-worker only (`risk/rules.py`)
- [ ] **F2** No account-level position count enforcement across workers — combined exposure can massively exceed configured limits
- [ ] **F3** `InternalSignalClient` bypasses `SignalValidator` — stale signals (older than `max_signal_age_ms`) execute without age-check (`signals/internal_client.py:106`)
- [ ] **F4** No deduplication in `InternalSignalClient` — the same signal can be delivered multiple times by the manager and processed each time, opening duplicate trades (`signals/internal_client.py:123`)
- [ ] **F5** `_emergency_close` in `order_manager.py:319` catches all exceptions and does NOT re-raise — the caller treats a failed close as success; an open position remains in the market silently
- [ ] **F6** TP1 partial close exception caught and logged (`positions/manager.py:377`) but `tp1_hit=True` is still written — store and DB record the hit even when zero lots were closed
- [ ] **F7** `positions/manager.py:447` — `be_sl` used after the block that assigns it; if `be_ok` is `True` but the outer `if` is `False`, raises `UnboundLocalError` at runtime
- [ ] **F8** `_widen_stops` does not check whether the widened SL still meets minimum R:R — trades execute with worse risk than approved (`execution/order_manager.py:330`)
- [ ] **F9** Partial fill: `plan.lot_size` used in `_emergency_close` instead of `filled_volume` — a partially filled position is over-closed
- [ ] **F10** No market hours check — signals during weekends/holidays exhaust all retries on `10018 MARKET_CLOSED` then are silently dropped
- [ ] **F11** `_RETRYABLE_RETCODES` includes `10007 TRADE_RETCODE_CANCEL` — intentional broker/user cancels are retried, potentially opening unwanted positions
- [ ] **F12** Daily loss tracker primed once at startup; a mid-day restart drops realized P&L for trades not in the local DB
- [ ] **F13** `position_poll_interval` defaults to 0.6 seconds — with multiple workers each polling MT5, the API is hammered; load test required before multi-worker deployment
- [ ] **F14** No maximum daily trade count limit; prop-firm rules that cap trade count are not enforced
- [ ] **F15** Close-reason classification uses the last polled price, not the actual deal price — positions closed intrabar can be misclassified as SL or TP

---

## 🟡 HIGH — reliability & correctness

### Thread safety
- [ ] **T1** `self._sequence += 1` in `WorkerEventClient._emit` is called from both `_snapshot_loop` and `_handle_command` threads with no lock — duplicate sequence numbers (`worker/event_client.py:211`)
- [ ] **T2** New `snapshot_thread` spawned on every `_connect_and_read()` call — on reconnect, the previous thread may still be running, causing two threads to emit snapshots simultaneously (`worker/event_client.py:112`)
- [ ] **T3** `_stub_miss_count` and `_last_price` in `PositionManager` mutated from poll thread and command thread concurrently without a lock (`positions/manager.py:55`)
- [ ] **T4** `positions/store.py:63,70` — `get_by_signal_id` and `get_by_ticket` return `copy.copy()` (shallow) — mutating the returned Trade's nested plan corrupts the store
- [ ] **T5** `positions/store.py:73` — `get_open_trades()` returns shallow copies; position manager modifies `current_lots` on these but nested objects are shared
- [ ] **T6** `core/event_bus.py:64` — `self._wildcard` is iterated directly (not a snapshot) — concurrent `on_any()` registration during `emit()` raises `RuntimeError`
- [ ] **T7** `signals/consumer.py` — `self._ws` is replaced in `_run_loop` without a lock while four other threads read it concurrently
- [ ] **T8** `worker/event_client.py:56,262` — `self._writer` and `self._socket` accessed from reader thread and snapshot thread; `_close_connection` sets them to `None` without `_send_lock`, creating TOCTOU race

### Error handling
- [ ] **E1** `_wire_events()` in `app/bootstrap.py:182` is not idempotent — MT5 reconnect calls it again and doubles every event handler, causing N-fold MT5 calls per event
- [ ] **E2** `app/bootstrap.py:167` — daily loss priming failure silently falls back to `0.0`; engine starts trading with miscalibrated loss tracker
- [ ] **E3** `app/bootstrap.py:203` — `_broadcast_mt5_error` swallows all its own exceptions with bare `except: pass`; MT5 boot errors can disappear entirely
- [ ] **E4** `infra/db.py:263` — `plan_json` serialization failure swallowed with bare `except: pass`; trade written with `plan_json=None` permanently
- [ ] **E5** `worker/event_client.py:256` — `_replay_outbox` fails mid-way if `_send_event` raises; partially-replayed outbox leaves inconsistent delivery state
- [ ] **E6** `core/event_bus.py:36` — `once()` wrapper removes itself even if the listener raises; listener silently never fires again after a first-call exception
- [ ] **E7** `worker/event_client.py:197` — `ENGINE_STOP` command executes `os.kill(os.getpid(), signal.SIGTERM)` which bypasses the `stop_event` drain — open positions may not be reconciled before exit
- [ ] **E8** Lifecycle queue full (`signals/consumer.py:391`) — event dropped, only `logger.error` emitted, no recovery path
- [ ] **E9** `signals/consumer.py:619` — corrupted outbox rows skipped silently and never cleaned up

### Timeouts
- [ ] **TO1** `mt5.initialize()` — no application-level timeout; frozen MT5 terminal blocks `mt5-connect` thread indefinitely (`brokers/mt5/client.py:51`)
- [ ] **TO2** `mt5.login()` — no timeout (`brokers/mt5/client.py:61`)
- [ ] **TO3** `worker/event_client.py:105` — `sock.settimeout(None)` after connect; a hung manager blocks the reader indefinitely
- [ ] **TO4** `infra/websocket.py:111` — `run_forever()` has no `ping_timeout`; a server accepting TCP but not pinging hangs the thread
- [ ] **TO5** `_build_snapshot` calls `get_account_info()` every 2 seconds holding `_MT5_LOCK`, contending with the 0.6s position poll

### IPC correctness
- [ ] **I1** `validate_envelope_timestamp` not called on events replayed from the worker outbox — stale events from hours ago replay verbatim (`worker/event_client.py:250`)
- [ ] **I2** `worker/event_client.py:119` — `reader.readline(MAX_WIRE_BYTES + 1)` reads the full line into memory before the size check; the guard doesn't prevent 1 MB allocations
- [ ] **I3** IPC protocol version not negotiated — manager/worker version mismatches are entirely undetected
- [ ] **I4** No rate limiting on inbound commands from the manager; a buggy manager floods the worker with `SIGNAL_DELIVER` commands
- [ ] **I5** `CONFIG_APPLY` raises `ValueError` and sends `COMMAND_REJECTED` with no automated worker restart mechanism; config changes are a purely manual operation

---

## 🔵 MEDIUM — observability, ops, configuration

### Logging & monitoring
- [ ] **O1** No trace/span IDs flowing through signal → IPC → worker → MT5; you cannot correlate a signal across the system
- [ ] **O2** No Prometheus endpoint or OpenTelemetry export; all metrics are internal counters
- [ ] **O3** Worker IPC disconnect logged at `DEBUG` — invisible in `INFO` mode (`worker/event_client.py:93`)
- [ ] **O4** Snapshot thread failures logged at `DEBUG` — telemetry gaps are invisible
- [ ] **O5** No alert/page on emergency close failure; a position stuck open is invisible until someone reads logs
- [ ] **O6** No alert on lifecycle queue full
- [ ] **O7** No performance timing on MT5 order execution; latency regressions are undetectable
- [ ] **O8** `main.ts:46` — `console.log` in gateway shutdown handler; replace with NestJS structured logger
- [ ] **O9** Log rotation not configured; logs grow until disk is full

### Resource leaks
- [ ] **R1** `core/event_bus.py:49` — no `off_any()` method; wildcard listeners accumulate on every MT5 reconnect (each `_wire_events()` call adds permanent duplicates)
- [ ] **R2** `positions/manager.py:57` — `_last_price` dict grows without bound; phantom entries for offline-closed tickets accumulate
- [ ] **R3** `infra/db.py:528` — `outbox_evict_sent()` never called from any scheduler; sent rows accumulate forever
- [ ] **R4** `worker/event_client.py:232` — worker outbox (`worker-events.db`) has no eviction; grows indefinitely if manager is unreachable
- [ ] **R5** `infra/db.py:349` — `load_all_trades_raw()` is `SELECT * FROM trades` with no `LIMIT`; will OOM on a long-running instance
- [ ] **R6** SQLite has no auto-vacuum; WAL bloat accumulates silently
- [ ] **R7** `_SYMBOL_CACHE` module-level global has no TTL; broker symbol renames produce stale resolutions until restart

### Configuration
- [ ] **C1** `config/settings.py:57` — `_INTERNAL_DEFAULTS` hardcodes `wss://apex-gateway.somicast.com/engine`; dev workers accidentally connect to production
- [ ] **C2** `__main__.py:46` — `manager_host` hardcoded to `"127.0.0.1"`; multi-host deployments require source changes
- [ ] **C3** `ENGINE_IPC_PORT` only configurable via env var, no config file path; undiscoverable to new operators
- [ ] **C4** `config/settings.py:139` — `position_poll_interval: 0.6s` default with multiple workers creates extreme MT5 API load; needs tuning guidance in docs
- [ ] **C5** `SIGNAL_ENGINE_WS_URL` defaults to `ws://localhost:8765` — plaintext, breaks in any non-local deployment
- [ ] **C6** `SMTP_SECURE` defaults to `false` — email transport is not enforced-TLS
- [ ] **C7** No startup validation that required env vars are present and non-empty; missing vars cause runtime failures, not boot failures
- [ ] **C8** No environment-specific config files (dev vs staging vs prod)
- [ ] **C9** `PAYSTACK_SECRET_KEY` appears in two separate gateway config keys; they will drift
- [ ] **C10** `version.txt` missing → version reports as `"0.1.0"`; version-based debugging is impossible

### Deployment
- [ ] **D1** No `Dockerfile` for the worker or manager
- [ ] **D2** No `docker-compose.yml` for the full stack (manager + workers + gateway)
- [ ] **D3** No health-check endpoint on the worker; liveness probes have nothing to call
- [ ] **D4** No process supervisor (systemd/PM2); a crashed worker does not restart
- [ ] **D5** Lock file at `config.storage_path/execution-engine.lock` not cleaned up on SIGKILL/power loss — next start fails with "Runtime already active" (`__main__.py:152`)
- [ ] **D6** No documented procedure for provisioning a new worker (engine_id, config, registration with manager, smoke test)
- [ ] **D7** Worker and manager must start in a specific order with no coordination; premature worker start silently retries IPC forever
- [ ] **D8** No rollback procedure for a faulty worker version
- [ ] **D9** No staging environment; changes go directly to production
- [ ] **D10** No memory/CPU limits; a runaway worker starves the host

### Persistence & backup
- [ ] **P1** No automated backup for `engine.db`; disk failure = total trade history loss
- [ ] **P2** No automated backup for `worker-events.db`
- [ ] **P3** No SQLite WAL checkpoint strategy; long-running WAL files slow queries over time
- [ ] **P4** Each worker has its own `engine.db`; no aggregate view of trades/P&L across workers without joining multiple databases manually
- [ ] **P5** No point-in-time recovery capability

---

## ⚪ LOW — polish & tech debt

- [ ] **L1** `positions/store.py:63,70,73,81` — change `copy.copy()` to `copy.deepcopy()` in `get_by_signal_id`, `get_by_ticket`, `get_open_trades`, `get_all`
- [ ] **L2** `calculate_realized_rr` uses string comparison `trade.side.value == "BUY"` instead of enum comparison — breaks silently if enum values change
- [ ] **L3** `comment=f"slippage-close"` in `order_manager.py:314` — unnecessary f-string with a commented-out interpolation; uncomment or clean up
- [ ] **L4** `_MT5_LOCK` and `_SYMBOL_CACHE` are module-level globals — shared across all `Mt5Client` instances in the same process; move to instance level
- [ ] **L5** `db._dpapi_warned` dynamically set on class instance in `save_device_state` — not a proper instance attribute; declare it in `__init__`
- [ ] **L6** `core/event_bus.py:49` — add `off_any()` method to allow wildcard listener removal
- [ ] **L7** `core/event_bus.py:33` — `once()` should not remove itself if the listener raises; wrap removal in `finally` or only remove on success
- [ ] **L8** `worker/event_client.py:250` — add `validate_envelope_timestamp` to outbox replay to skip events older than `MAX_ENVELOPE_AGE_MS`
- [ ] **L9** `worker/event_client.py:232` — add outbox eviction (e.g. delete acknowledged rows older than 24h on startup)
- [ ] **L10** `infra/db.py:349` — add `LIMIT` parameter to `load_all_trades_raw()`; callers that need all trades should paginate
- [ ] **L11** `infra/db.py:624` — pass explicit `timeout` to `sqlite3.connect()` to match expected WAL contention window
- [ ] **L12** `_resolve_engine_version` returns `"0.1.0"` as fallback — raise or log a warning instead of silently reporting wrong version
- [ ] **L13** No `.env.example` file in `execution-engine-multi/` — new developers have no reference for required variables
- [ ] **L14** `signals/consumer.py:84` — `_seen_ids` dedup state is lost on restart; document this limitation or add persistence
- [ ] **L15** `venv/` should be in `.gitignore`; replace with a proper `requirements.txt` or `pyproject.toml` + lock file

---

## Tests to write (before any deployment)

- [ ] **TS1** `WorkerEventClient` — connect, receive command, send event, disconnect/reconnect
- [ ] **TS2** `WorkerEventClient` — `SIGNAL_DELIVER` command delivers signal to event bus
- [ ] **TS3** `WorkerEventClient` — outbox: persist on disconnect, replay on reconnect, ack on delivery
- [ ] **TS4** `WorkerEventClient` — concurrent `_sequence` increment (race condition check)
- [ ] **TS5** `InternalSignalClient` — subscribe, receive signal, emit to bus
- [ ] **TS6** `InternalSignalClient` — duplicate signal delivered twice → emits twice (document current behaviour, then fix F4)
- [ ] **TS7** `contracts.py` — `from_wire/to_wire` round-trips for all command/event types
- [ ] **TS8** `validate_envelope_timestamp` — stale, future, and valid timestamps
- [ ] **TS9** `OrderManager.execute_market_order` — happy path, requote retry, NO_MONEY halving, slippage rejection
- [ ] **TS10** `PositionManager._handle_tp1_price_reached` — partial close succeeds, partial close fails (tp1_hit still set?), BE move
- [ ] **TS11** `PositionManager.emergency_close_all` — closes all positions, handles individual close failure
- [ ] **TS12** `PositionManager._handle_position_gone` — TP2 classification, SL classification, manual classification
- [ ] **TS13** `RiskEngine.evaluate` — all 10 rules in combination; cross-worker aggregate risk
- [ ] **TS14** `SignalConsumer._replay_outbox` — reconnect replays pending, skips delivered
- [ ] **TS15** `_wire_events` idempotency — calling twice does not duplicate handlers
- [ ] **TS16** End-to-end: signal received via IPC → risk check → order sent → TP1 hit → TP2 hit
- [ ] **TS17** Load test: N workers, M signals/sec, verify no duplicate executions and no cross-worker risk breach

---

## External actions (cannot be automated)

| # | Action | Who | Status |
|---|--------|-----|--------|
| X1 | Rotate `SUPABASE_SERVICE_ROLE_KEY` in Supabase dashboard | Infra | ⬜ |
| X2 | Rotate `DATABASE_URL` password in Supabase dashboard | Infra | ⬜ |
| X3 | Rotate `SMTP_PASS` in email provider | Infra | ⬜ |
| X4 | Switch `PAYSTACK_SECRET_KEY` from `sk_test_` to live key | Infra | ⬜ |
| X5 | Rotate `GATEWAY_ADMIN_KEY` and update all `.env` files | Infra | ⬜ |
| X6 | Enable Supabase Row Level Security on all tables | Infra | ⬜ |
| X7 | Configure production SMTP provider (Resend, SES, Postmark) | Infra | ⬜ |
| X8 | Set `GATEWAY_CORS_ORIGIN` to production dashboard URL | Infra | ⬜ |
| X9 | Add WAF rules (Cloudflare or equivalent) on gateway endpoints | Infra | ⬜ |
| X10 | Set up log aggregation (Datadog, Loki, etc.) | Infra | ⬜ |
| X11 | Set up on-call alerting for critical failures | Ops | ⬜ |
| X12 | Legal/compliance review for automated multi-account trading | Legal | ⬜ |
| X13 | Load test: multiple workers against a demo MT5 account | QA | ⬜ |
| X14 | Penetration test on gateway HTTP + WS endpoints | Security | ⬜ |

---

## Suggested fix order

```
Week 1 — Blockers (B1–B7)
  → Get the manager package into the repo and the worker entry point working

Week 2 — Secrets + IPC security (S1–S19)
  → Rotate credentials, add TLS to IPC, token from env, validate signals

Week 3 — Financial correctness (F1–F15)
  → Cross-worker risk, dedup on internal path, emergency close, TP1 fix

Week 4 — Thread safety (T1–T8)
  → Locks, deepcopy, event bus snapshot, _wire_events idempotency

Week 5 — Tests (TS1–TS17)
  → Cannot ship without WorkerEventClient and cross-worker risk tests

Week 6 — Ops/deployment (D1–D10, P1–P5, O1–O9)
  → Docker, health checks, supervisor, backups, monitoring

Week 7 — Configuration, polish, external actions (C1–C10, L1–L15, X1–X14)
```
