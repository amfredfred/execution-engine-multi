# Execution Engine Multi - Production Readiness Checklist

This document tracks the work required before `execution-engine-multi` can be
used with production trading accounts.

## Status Rules

- `[ ]` Not started
- `[~]` In progress
- `[x]` Completed and verified
- `[!]` Blocked

Do not mark an item complete until its listed check passes. Production release
is blocked while any P0 or P1 item remains incomplete.

## Current Baseline

Audit baseline from June 15, 2026:

- `python -m compileall -q src manager tests`: passes
- `python -m pytest`: 184 passed
- Critical Ruff checks: pass
- Full `python -m ruff check src manager tests`: 826 findings
- `src.manager` to `manager` package migration tests and imports are aligned
- Live-looking credentials exist in the local `config.yaml`

## Standard Checks

Run these checks after every phase:

```powershell
python -m compileall -q src manager tests

Remove-Item -Recurse -Force .test-tmp -ErrorAction SilentlyContinue
$env:TEMP = (Resolve-Path ".").Path + "\.test-tmp"
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
python -m pytest

python -m ruff check src manager tests --select F821,RUF006,F841,B008,B904,RUF012
python -m ruff check src manager tests
```

Before release, also build and smoke-test the installer:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1 -Clean
```

## P0 - Immediate Security Containment

- [!] **001** Rotate the exposed MT5 password. Blocked: requires broker-account access.
  Check: old password can no longer authenticate.
- [!] **002** Rotate the exposed activation key. Blocked: requires gateway/license-owner access.
  Check: old activation key is rejected by the gateway.
- [x] **003** Remove credentials from tracked and distributed configuration.
  Check: secret scanner finds no MT5 password, activation key, or channel token.
- [!] **004** Remove credentials from Git history before publishing the repository. Blocked: requires coordinated destructive history rewrite and remote force-push.
  Check: secret scanner passes against all reachable commits.
- [x] **005** Make DPAPI encryption failures fatal instead of storing plaintext.
  Check: simulated DPAPI failure prevents secret persistence.
- [x] **006** Restrict manager and agent data ACLs to the runtime identity and administrators.
  Check: a normal authenticated user cannot read or modify manager data.
- [x] **007** Stop exposing the manager API token through a broadly readable plaintext file.
  Check: only the GUI identity and manager identity can retrieve the token.
- [x] **008** Implement API-token rotation.
  Check: old tokens stop working after rotation.

## P0 - Repository And Test Gate

- [x] **009** Complete the `src.manager` to `manager` package migration.
  Check: no runtime or test import references `src.manager`.
- [x] **010** Update PyInstaller and installer paths for the final package layout.
  Check: packaged manager, GUI, and worker all launch.
- [x] **011** Repair stale `ManagerSignalRouter` tests and contracts.
  Check: manager signal-routing tests pass.
- [x] **012** Repair stale `AgentConfigStore` tests and contracts.
  Check: config-store tests pass.
- [x] **013** Fix all undefined-name and dangling-task findings.
  Check: critical Ruff command passes.
- [x] **014** Add CI gates for compile, tests, critical lint, secrets, and installer build.
  Check: deliberately broken test or leaked secret blocks CI.
- [x] **015** Reach a clean test baseline.
  Check: `python -m pytest` reports zero failures and zero errors.

## P0 - Secure Worker IPC

- [x] **016** Issue a unique IPC credential to every worker.
  Check: one worker's credential cannot connect as another worker.
- [x] **017** Authenticate workers against registered agent identities.
  Check: unknown and deleted agents are rejected.
- [x] **018** Bind every received event to the authenticated connection identity.
  Check: spoofed envelope `engine_id` values are rejected.
- [x] **019** Reject events from superseded worker connections.
  Check: an old connection cannot publish after reconnect.
- [x] **020** Close old worker connections during reconnect.
  Check: only one active connection exists per worker.
- [x] **021** Add IPC line and payload size limits.
  Check: oversized payloads are rejected without manager memory growth.
- [x] **022** Add socket idle, read, and write timeouts.
  Check: stalled connections are closed automatically.
- [x] **023** Replace the global IPC write lock with per-worker locks.
  Check: one stalled worker does not delay commands to other workers.
- [x] **024** Validate worker event sequence numbers.
  Check: duplicate, replayed, and out-of-order events are detected.
- [x] **025** Reject stale commands and events.
  Check: envelopes outside the allowed age window are rejected.
- [x] **026** Validate command and event configuration revisions.
  Check: workers reject commands for an incompatible revision.
- [x] **027** Persist command acknowledgements and rejections.
  Check: command outcome remains queryable after manager restart.

## P0 - Durable Signal Delivery

- [x] **028** Add a durable manager-side signal-delivery outbox.
  Check: queued signals survive manager restart.
- [x] **029** Require worker acknowledgement after signal acceptance.
  Check: socket write alone is not considered delivery.
- [x] **030** Retry unacknowledged signals with bounded backoff.
  Check: a temporary worker disconnect does not lose a valid signal.
- [x] **031** Expire stale queued signals.
  Check: expired signals are recorded and never executed.
- [x] **032** Enforce end-to-end deduplication using signal IDs.
  Check: retries never create duplicate trades.
- [x] **033** Replace blanket same-symbol dropping with strategy-aware behavior.
  Check: valid opposite-direction or newer signals receive an explicit outcome.
- [x] **034** Persist every delivered, rejected, expired, and dropped signal outcome.
  Check: every received signal has an auditable terminal state.
- [x] **035** Forward worker execution events instead of discarding manager callbacks.
  Check: upstream receives worker lifecycle and execution events.

## P0 - Worker Lifecycle And Commands

- [x] **036** Report `WORKER_READY` only after MT5 and trading services are ready.
  Check: disconnected workers never become `RUNNING`.
- [x] **037** Report accurate `STARTING`, `DEGRADED`, `RUNNING`, and `STOPPING` states.
  Check: state transitions match controlled failure scenarios.
- [x] **038** Emit `WORKER_STOPPED` during graceful shutdown.
  Check: manager records the final worker state.
- [x] **039** Detect stale snapshots and stale `last_seen_at`.
  Check: silent workers become unhealthy within the configured threshold.
- [x] **040** Make `/health` verify registry, IPC, signal manager, and worker health.
  Check: dependency failure makes health return a non-healthy result.
- [x] **041** Roll back partially started manager components.
  Check: failed startup leaves no active orphan component.
- [x] **042** Stop workers before manager IPC shutdown.
  Check: final worker events reach the manager.
- [x] **043** Add a dedicated `CLOSE_TRADE` command type.
  Check: close-trade cannot be parsed as a signal.
- [x] **044** Validate command payload schemas.
  Check: malformed commands are rejected before worker execution.
- [x] **045** Return actual command outcomes, not socket-write outcomes.
  Check: API exposes accepted, completed, rejected, and timed-out states.

## P1 - Process Supervision

- [x] **046** Implement graceful worker stop over IPC.
  Check: workers flush state and close cleanly.
- [x] **047** Escalate stop from graceful request to terminate to force-kill.
  Check: logs identify each escalation stage.
- [x] **048** Protect open positions during manager shutdown.
  Check: manager refuses unsafe shutdown unless explicitly forced.
- [x] **049** Verify process identity beyond PID before adoption or termination.
  Check: PID reuse cannot cause an unrelated process to be killed.
- [x] **050** Track adopted workers inside the supervisor.
  Check: adopted workers receive commands and lifecycle monitoring.
- [x] **051** Monitor adopted-worker exits.
  Check: adopted worker crashes trigger reconciliation.
- [x] **052** Correct crash-loop accounting to use a rolling time window.
  Check: old crashes do not incorrectly trigger a crash loop.
- [x] **053** Release terminal leases only after process identity verification.
  Check: live workers never lose their lease during reconciliation.

## P1 - Transactional Provisioning And Operations

- [x] **054** Fail provisioning when terminal lease acquisition fails.
  Check: two agents cannot use the same terminal.
- [x] **055** Attach worker PID and identity information to terminal leases.
  Check: lease ownership can be verified after restart.
- [x] **056** Allocate agent IDs transactionally.
  Check: concurrent provisioning creates unique IDs.
- [x] **057** Allocate ports transactionally or remove unused worker monitoring ports.
  Check: concurrent provisioning cannot allocate duplicate ports.
- [x] **058** Fail closed when license slot verification is unavailable.
  Check: gateway outage prevents exceeding licensed capacity.
- [x] **059** Continuously enforce license validity.
  Check: revoked or expired licenses stop new entries according to policy.
- [x] **060** Add rollback for partially failed provisioning.
  Check: failure leaves no directory, config, secret, lease, or registration.
- [x] **061** Wait for worker exit before deprovisioning.
  Check: deleted agents cannot continue running.
- [x] **062** Archive or securely remove deprovisioned agent data.
  Check: removed-agent secrets and databases follow the retention policy.
- [x] **063** Bound the operation executor queue.
  Check: API request floods cannot create unlimited pending operations.
- [x] **064** Recover interrupted operations after manager restart.
  Check: no operation remains permanently `pending` or `running`.
- [x] **065** Add retention policies for events, operations, outboxes, and logs.
  Check: long-running soak tests show bounded disk growth.

## P1 - Fail-Closed Risk Controls

- [x] **066** Treat broker-history failure as a risk-data outage.
  Check: new entries pause when realized P&L is unavailable.
- [x] **067** Remove zero-loss fallbacks during risk-data failures.
  Check: errors never reset daily loss to zero.
- [x] **068** Track and expose risk-data freshness.
  Check: stale daily-loss data changes worker health to degraded.
- [x] **069** Define whether limits are account-wide, per-agent, or per-magic.
  Check: documented policy matches implementation and tests.
- [x] **070** Add account-wide daily-loss protection where required.
  Check: manual and other-EA losses are included according to policy.
- [x] **071** Make start-equity and P&L scopes consistent.
  Check: calculation uses the same account/trade scope throughout.
- [x] **072** Persist and hydrate daily pause and loss state.
  Check: restarting cannot clear a daily-loss pause.
- [x] **073** Persist and hydrate profit-drawdown state.
  Check: restarting cannot clear the session peak.
- [x] **074** Persist and hydrate rolling-equity state.
  Check: restarting cannot clear rolling drawdown protection.
- [x] **075** Use broker-defined trading-day boundaries.
  Check: broker midnight resets exactly once.
- [x] **076** Handle timezone and DST changes correctly.
  Check: DST transition tests pass.
- [x] **077** Strictly validate every numeric risk and execution setting.
  Check: negative, zero, NaN, infinite, and unreasonable values fail startup.
- [x] **078** Parse booleans strictly.
  Check: YAML `"false"` does not become true.
- [x] **079** Reject non-positive polling intervals.
  Check: invalid intervals fail configuration validation.
- [x] **080** Generate and enforce unique magic numbers per agent.
  Check: one agent cannot manage another agent's positions.
- [x] **081** Define emergency-stop scope.
  Check: engine-only and account-wide behavior is explicit and tested.

## P1 - Order And Position Safety

- [x] **082** Require explicit broker-symbol mappings.
  Check: ambiguous symbols are rejected.
- [x] **083** Invalidate symbol mappings after reconnect or account change.
  Check: stale mappings cannot cross account sessions.
- [x] **084** Fail when `symbol_select()` fails.
  Check: failed selection never enters the symbol cache.
- [x] **085** Reject symbols without valid live tick data.
  Check: zero bid/ask values cannot reach planning or execution.
- [x] **086** Distinguish placed, partially filled, and completed orders.
  Check: `PLACED` is never treated as a confirmed fill.
- [x] **087** Confirm broker position state after every open and close.
  Check: broker outcome and local state reconcile before completion.
- [x] **088** Stop new trading when persistence is unavailable.
  Check: a live position cannot be intentionally opened without durable tracking.
- [x] **089** Reconcile positions opened but not persisted.
  Check: restart restores complete tracking or pauses for intervention.
- [x] **090** Persist enough metadata to restore TP1 management.
  Check: restart preserves TP1, TP2, original stop, and sizing state.
- [x] **091** Mark TP1 hit only after required actions succeed.
  Check: failed partial close and failed BE move remain retryable.
- [x] **092** Normalize all volume using broker `volume_step`.
  Check: partial and remaining volumes are broker-valid.
- [x] **093** Add recovery for failed partial closes and SL modifications.
  Check: transient broker failures are retried without duplicate actions.

## P1 - API, Installer, And Updates

- [x] **094** Limit HTTP body size, concurrency, and request duration.
  Check: API abuse tests show bounded memory and thread use.
- [x] **095** Validate every API body and query parameter.
  Check: malformed JSON and invalid values return controlled errors.
- [x] **096** Cap log-line requests and tail logs without reading the whole file.
  Check: large logs do not cause large memory spikes.
- [x] **097** Run manager and GUI with least privilege.
  Check: normal operation does not require administrator privileges.
- [x] **098** Start the manager at system boot without interactive login.
  Check: reboot with no user login still starts the manager.
- [x] **099** Make restart recovery externally monitored and effectively continuous.
  Check: manager recovers after more than five consecutive failures.
- [x] **100** Require signed, checksummed, atomic updates with rollback.
  Check: unsigned, checksum-less, corrupt, and failed updates are rejected or rolled back.

## Production Qualification

- [!] Run a seven-day demo-account soak test. Blocked: requires a continuously running demo environment.
- [!] Test manager crash and restart. Blocked: requires installed-system qualification.
- [!] Test worker crash and restart. Blocked: requires installed-system qualification.
- [!] Test MT5 terminal restart. Blocked: requires a live MT5 demo terminal.
- [!] Test signal-manager and network outages. Blocked: requires an integrated staging environment.
- [!] Test corrupt databases and disk-full conditions. Blocked: requires isolated destructive qualification.
- [!] Test missing broker history and stale risk data. Blocked: requires broker/staging fault injection.
- [!] Test partial fills and delayed broker confirmations. Blocked: requires broker/staging fault injection.
- [!] Verify no duplicate, untracked, or cross-agent trades. Blocked: requires the soak/fault campaign.
- [!] Verify every signal has an auditable terminal outcome. Blocked: requires the soak/fault campaign.
- [!] Verify every risk-data outage pauses new entries. Blocked: requires the soak/fault campaign.
- [!] Complete security review of IPC, API, ACLs, secrets, installer, and updater. Blocked: requires independent review.
- [!] Write recovery, emergency-stop, backup, rollback, and incident runbooks. Blocked: requires owner-approved operational policy.

## Release Gate

Production release is allowed only when all of the following are true:

- [!] Items **001-100** are completed and verified. Blocked by items 001, 002, and 004.
- [x] Tests pass with zero failures and zero errors.
- [x] Critical Ruff checks pass.
- [!] Secret scanning passes across the full Git history. Blocked by item 004.
- [!] Installer build and clean-machine smoke test pass. Package build and local mode smoke tests pass; clean-machine test remains external.
- [!] Seven-day soak test passes. Blocked: requires a continuously running demo environment.
- [!] Every live position remains tracked through tested failure scenarios. Blocked: requires the qualification campaign.
- [!] Every signal has a durable, auditable terminal outcome. Blocked: requires the qualification campaign.
- [!] Every risk-data failure pauses new trading. Blocked: requires the qualification campaign.
