# Execution-Engine-Multi — Architectural Clean-up & GUI Completion Plan

## 1. What is correct today

| Concern | Status |
|---|---|
| Signals: Manager → Worker via IPC `SIGNAL_DELIVER` | ✅ Already correct |
| Workers do NOT start their `SignalConsumer` gateway WS | ✅ Already correct — `if not container.worker_events` guard in bootstrap |
| Workers do NOT expose UIBridge WebSocket | ✅ Already correct — `expose_local_ui=False` in `_worker_main` |
| Manager connects to Signal Manager (port 8765) via `InternalSignalClient` | ✅ Already correct |
| GUI → Manager REST API (port 8870) only | ✅ Already correct |
| Workers report state to Manager via IPC (EngineEventHub) | ✅ Already correct |

## 2. What is wrong or missing

### 2a. Folder structure — `Multi/` level must be removed

**Current ProgramData layout:**
```
C:\ProgramData\Apex Quantel\
  Multi\
    manager\          ← manager storage, logs, tokens
    agents\
      {agent_id}\     ← per-agent config, logs, db
```

**Target layout (manager at root, agents under manager):**
```
C:\ProgramData\Apex Quantel\
  manager\            ← manager storage, logs, tokens
    agents\
      {agent_id}\     ← per-agent config, logs, db
```

Files to update:
- `src/config/settings.py` → `ManagerConfig.defaults()` — remove `/ "Multi"`, change `agents_data_dir` to `str(base / "manager" / "agents")`
- `src/gui/config_manager.py` → `_PROGDATA*` and `programdata_logs_path()` / `programdata_data_path()` to match
- `installer/ApexQuantel.iss` → `[Dirs]` section — update paths
- `install_manager.ps1` — update any hardcoded paths that reference `Multi\`

### 2b. Worker container has UIBridge dependency it shouldn't need

`WorkerEventClient._build_snapshot()` calls `self._container.ui_bridge.build_remote_snapshot()`. UIBridge is created in `bootstrap()` even for workers (just not started). This means UIBridge is instantiated in every worker for the sole purpose of building a telemetry dict.

**Fix:** Replace the `ui_bridge.build_remote_snapshot()` call in `_build_snapshot` with a direct `_build_worker_telemetry(container)` helper that reads from the container's components directly (mt5_positions, position_store, loss_tracker, cluster_tracker, equity_throttle). Then `AppContainer.ui_bridge` can remain `None` in worker mode — no UIBridge is instantiated at all.

### 2c. Worker container still builds `SignalConsumer` (unnecessary for workers)

`build_container()` always constructs a `SignalConsumer` (which holds a `WebSocketClient` reference). For workers the consumer is never started, but it's wired with `set_snapshot_provider` in bootstrap. Clean this up: skip `SignalConsumer` construction when in worker mode, OR keep it but don't wire `set_snapshot_provider` (since UIBridge won't exist).

The cleanest approach: keep the current structure but make `bootstrap()` skip `set_snapshot_provider` when `expose_local_ui=False`. No `SignalConsumer` gateway WS client will ever open.

### 2d. Wrong-architecture GUI files to delete

- `src/gui/ws_client.py` — delete (direct WS to worker, never needed)
- `src/gui/pages/agent_dashboard.py` — delete current version (connects to `monitoring_port` directly, violates architecture)

### 2e. Missing GUI pages (must port from single-agent and adapt)

Pages that exist in `execution-engine/src/gui/pages/` but are missing from `execution-engine-multi/src/gui/pages/`:

| Page | Source | Multi-agent adaptation |
|---|---|---|
| `settings.py` | Copy, adapt | Remove "auto-start engine" (manager is a scheduled task). Fix folder paths to new multi-agent paths. Remove `restart_with_new_config()` call. Remove license section (lives in Manager page). |
| `risk.py` | Copy, adapt | Remove `self.app.restart_with_new_config()`. Add info label: "These are default risk settings applied to new agents." Save via `self.app.config.update("risk", ...)` as before. |
| `logs.py` | Rewrite | Multi-agent: shows manager log from `{manager_storage}/logs/manager.log`. Simple file tail. No WebSocket, no config path detection. |
| `activity.py` | Adapt | Events come from `manager_state` (subscribe to "agents" event, diff agent statuses to generate fleet-level events). Raw logs tab reads manager log. No WebSocket callbacks. |

### 2f. Missing Manager API endpoints

The GUI needs these endpoints to build the agent dashboard and forward commands:

| Endpoint | Purpose |
|---|---|
| `GET /agents/{id}/logs?lines=200` | Read last N lines of agent's log file from disk |
| `POST /agents/{id}/command` | Proxy `pause`, `resume`, `close_trade {trade_id}`, `emergency_stop` to worker via `event_hub.send_command()` |

### 2g. Agent dashboard page (correct architecture)

New `src/gui/pages/agent_dashboard.py` — polls manager API only:
- Header: agent name, status badge, MT5 login/server
- Metrics section: balance, equity, open trades, uptime, gateway status
- Controls: Pause / Resume / Emergency Stop → `POST /agents/{id}/command`
- Logs section: `GET /agents/{id}/logs` on open, refresh button
- Back button: navigates back to Agents page (`manager_state.clear_selection()`)

Data source: `app.manager_state.get_agent(id)` for the current snapshot (refreshed by the existing 3-second poll in `ManagerClient`). No direct WebSocket, no `monitoring_port` connection.

### 2h. App.py nav registration

Add nav buttons and page registrations for: **Settings**, **Risk**, **Logs**, **Activity**.  
Agent Dashboard is registered but NOT in the sidebar (navigated to via Open button on an agent card).

---

## 3. Execution order

1. **Folder paths** — update `settings.py` (ManagerConfig), `config_manager.py` (GUI), installer `.iss`, `install_manager.ps1`. Rebuild installer.
2. **Worker telemetry cleanup** — replace `ui_bridge.build_remote_snapshot()` in `WorkerEventClient._build_snapshot` with a direct helper; remove UIBridge instantiation from `bootstrap()` in worker mode.
3. **Delete wrong files** — `ws_client.py`, `agent_dashboard.py` (current version).
4. **Manager API additions** — add `GET /agents/{id}/logs` and `POST /agents/{id}/command` in `api.py`.
5. **Port GUI pages** — write `settings.py`, `risk.py`, `logs.py`, `activity.py` (adapted for multi-agent).
6. **Agent dashboard** — write new `agent_dashboard.py` using manager API only.
7. **App.py** — register pages, update nav.
8. **Build and test** — run `installer/build.ps1`, smoke-test.

---

## 4. Files touched (summary)

| File | Action |
|---|---|
| `src/config/settings.py` | Change ManagerConfig paths — remove `Multi/` |
| `src/gui/config_manager.py` | Update `programdata_logs_path()` / `programdata_data_path()` |
| `installer/ApexQuantel.iss` | Update `[Dirs]` paths |
| `install_manager.ps1` | Update any hardcoded `Multi\` references |
| `src/worker/event_client.py` | Replace `ui_bridge.build_remote_snapshot()` with direct helper |
| `src/app/bootstrap.py` | Skip `set_snapshot_provider` when `expose_local_ui=False` |
| `src/app/container.py` | `ui_bridge` stays `None` in worker mode (no change needed, just confirm) |
| `src/manager/api.py` | Add `/agents/{id}/logs` and `/agents/{id}/command` endpoints |
| `src/gui/ws_client.py` | **DELETE** |
| `src/gui/pages/agent_dashboard.py` | **DELETE then REWRITE** (manager API only) |
| `src/gui/pages/settings.py` | **CREATE** (port + adapt) |
| `src/gui/pages/risk.py` | **CREATE** (port + adapt) |
| `src/gui/pages/logs.py` | **CREATE** (manager log viewer) |
| `src/gui/pages/activity.py` | **CREATE** (fleet events from manager_state) |
| `src/gui/app.py` | Register new pages, update nav |

---

## 5. What is NOT changing

- Worker IPC protocol (EngineEventHub / WorkerEventClient) — already correct
- Manager signal routing (ManagerSignalRouter → InternalSignalClient → Signal Manager) — already correct  
- UIBridge code itself — still used by the standalone single-agent engine, not touched
- `execution-engine/` (single-agent) — not touched at all
- Signal-engine — not touched
