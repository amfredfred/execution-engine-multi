"""
Connects to the Apex Quantel Gateway WebSocket, deserialises messages,
validates them, and emits them onto the EventBus.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import platform
import queue
import socket
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable, Optional
from uuid import uuid4

from src.core.events import Events
from src.domain.signal_interface import InboundSignal
from src.infra.metrics import metrics
from src.infra.websocket import WebSocketClient
from src.utils.time import now_ms

from .signal_types import (
    SIGNAL_CLOSE_EVENTS,
    SIGNAL_TRIGGER_EVENTS,
    is_valid_signal_dict,
)

if TYPE_CHECKING:
    from src.core.event_bus import EventBus
    from src.infra.db import Database

    from .signal_validator import SignalValidator

logger = logging.getLogger(__name__)


class SignalConsumer:
    """
    Joins Gateway symbol rooms and routes validated signals to the bus.

    Thread model: `WebSocketClient` runs in a daemon thread.
    `on_message` is therefore called from that thread — the EventBus
    dispatches synchronously, so all downstream handlers execute there too.
    If downstream handlers are long-running, consider dispatching to a queue.
    """

    # Maximum number of signal IDs to retain for deduplication.
    # Oldest entries are evicted once this limit is reached.
    _SEEN_IDS_MAX = 10_000

    def __init__(
        self,
        event_bus: EventBus,
        validator: SignalValidator,
        ws_url: str,
        activation_key: str,
        symbols: list[str],
        engine_id: str,
        engine_version: str,
        room_ttl_seconds: int,
        account_login: str,
        signal_hmac_secret: Optional[str] = None,
        db: Optional["Database"] = None,
    ) -> None:
        self._bus = event_bus
        self._validator = validator
        self._symbols = symbols
        self._activation_key = activation_key
        self._engine_id = engine_id
        self._engine_version = engine_version
        self._room_ttl_seconds = room_ttl_seconds
        self._account_login = account_login
        self._signal_hmac_secret: Optional[bytes] = (
            signal_hmac_secret.encode() if signal_hmac_secret else None
        )
        # 2.10 — Bounded seen-IDs set for deduplication keyed by (event, signal_id)
        # Using a tuple key prevents lifecycle events (pending → triggered) with the
        # same signal_id from being incorrectly dropped as duplicates.
        self._seen_ids: OrderedDict[tuple[str, str], None] = OrderedDict()
        # 2.11 — Reliable event outbox
        self._db: Optional["Database"] = db
        # 1.16 — Device credential: loaded from local DB, presented in engine.hello
        # for fast-path reconnect.  Updated whenever the gateway issues a new one.
        self._device_credential: Optional[str] = None
        if db is not None:
            try:
                self._device_credential = db.load_device_state("device_credential")
                if self._device_credential:
                    logger.info("Device credential loaded from local store")
            except Exception:
                logger.warning("Could not load device credential from DB")
        self._started_at = self._utc_now()
        self._stopped = threading.Event()
        self._activated = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._lifecycle_thread: threading.Thread | None = None
        # Each item is (event, report, outbox_id_or_None)
        self._lifecycle_queue: queue.Queue[tuple[str, dict, Optional[int]]] = queue.Queue(maxsize=1000)
        self._heartbeat_sequence = 0
        self._metrics_subscribed = threading.Event()
        self._snapshot_provider: Callable[[], dict] | None = None
        self._execution_event_sink: Callable[[str, dict], None] | None = None
        self._metrics_thread: threading.Thread | None = None
        self._hello_message_id: str | None = None
        self._activation_message_id: str | None = None
        # Remote command callbacks — registered by bootstrap after wiring
        self._on_pause: Optional[Callable[[], None]] = None
        self._on_resume: Optional[Callable[[], None]] = None
        self._on_emergency_stop: Optional[Callable[[], None]] = None
        self._ws = WebSocketClient(
            url=ws_url,
            on_message=self._handle_raw,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        logger.info("SignalConsumer starting", extra={"symbols": self._symbols})
        self._stopped.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_rooms,
            name="gateway-room-refresh",
            daemon=True,
        )
        self._refresh_thread.start()
        self._heartbeat_thread = threading.Thread(
            target=self._send_heartbeats,
            name="gateway-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        self._lifecycle_thread = threading.Thread(
            target=self._send_lifecycle_reports,
            name="gateway-lifecycle-reporter",
            daemon=True,
        )
        self._lifecycle_thread.start()
        self._metrics_thread = threading.Thread(
            target=self._send_metrics_snapshots,
            name="gateway-metrics-reporter",
            daemon=True,
        )
        self._metrics_thread.start()
        self._ws.start()

    def stop(self) -> None:
        self._stopped.set()
        self._ws.stop()

    def set_snapshot_provider(self, provider: Callable[[], dict]) -> None:
        self._snapshot_provider = provider

    def set_execution_event_sink(self, sink: Callable[[str, dict], None]) -> None:
        """Route execution events through a manager-owned connection."""
        self._execution_event_sink = sink

    def set_command_callbacks(
        self,
        on_pause: Callable[[], None],
        on_resume: Callable[[], None],
        on_emergency_stop: Callable[[], None],
    ) -> None:
        """
        Register callables that are invoked when the gateway sends remote
        command events to this engine.  Call this once in bootstrap after all
        components are wired.
        """
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_emergency_stop = on_emergency_stop

    # ── Private ───────────────────────────────────────────────────────────

    def _on_connected(self) -> None:
        self._activated.clear()
        # 1.16 — Include device credential if available for fast-path activation
        hello_payload: dict = {
            "engine_id": self._engine_id,
            "engine_version": self._engine_version,
            "protocol_versions": ["1.0"],
            "started_at": self._started_at,
            "accounts": [],
        }
        if self._device_credential:
            hello_payload["device_credential"] = self._device_credential
        self._hello_message_id = self._send("engine.hello", hello_payload)

    def _activate(self) -> None:
        architecture = "arm64" if platform.machine().lower() == "arm64" else "x64"
        self._activation_message_id = self._send(
            "activation.request",
            {
                "activation_key": self._activation_key,
                "device_name": socket.gethostname(),
                "engine_version": self._engine_version,
                "platform": {"os": "windows", "architecture": architecture},
                "mt5_accounts": [],
            },
        )

    def _subscribe(self) -> None:
        sent = self._send(
            "room.subscribe",
            {
                "engine_id": self._engine_id,
                "symbols": self._symbols,
                "ttl_seconds": self._room_ttl_seconds,
            },
        )
        if sent:
            logger.info(
                "SignalConsumer joined Gateway rooms",
                extra={
                    "engine_id": self._engine_id,
                    "symbols": self._symbols,
                    "ttl_seconds": self._room_ttl_seconds,
                },
            )

    def _on_disconnected(self) -> None:
        self._activated.clear()
        self._metrics_subscribed.clear()
        logger.warning("Apex Quantel Gateway disconnected")

    def _refresh_rooms(self) -> None:
        # Cap at 300 s so a silently-evicted room is re-joined within 5 minutes.
        # (Gateway restart already triggers an immediate re-subscribe via the
        # reconnect → activation.accepted path, so this only covers in-connection
        # eviction edge cases.)
        refresh_interval = max(15.0, min(300.0, self._room_ttl_seconds / 2))
        while not self._stopped.wait(refresh_interval):
            if self._activated.is_set():
                self._subscribe()

    def _send_heartbeats(self) -> None:
        while not self._stopped.wait(30.0):
            if self._activated.is_set():
                self._heartbeat()

    def _send_metrics_snapshots(self) -> None:
        while not self._stopped.wait(1.5):
            if (
                self._activated.is_set()
                and self._metrics_subscribed.is_set()
                and self._snapshot_provider
            ):
                self._send("execution.metrics.snapshot", self._snapshot_provider())

    def _heartbeat(self) -> None:
        self._heartbeat_sequence += 1
        self._send(
            "engine.heartbeat",
            {
                "engine_id": self._engine_id,
                "status": "running",
                "sequence": self._heartbeat_sequence,
                "observed_at": self._utc_now(),
            },
        )

    def report_event(self, event: str, payload: object) -> None:
        stage: str | None = None
        signal_id: str | None = None
        reason: str | None = None
        trade_id: str | None = None
        broker_ticket: str | None = None

        if event == Events.SIGNAL_RECEIVED:
            if payload["event"] in SIGNAL_TRIGGER_EVENTS:  # type: ignore[index]
                stage = "accepted"
                signal_id = payload["signal"].id  # type: ignore[index]
        elif event in {Events.SIGNAL_REJECTED, Events.RISK_REJECTED}:
            stage = "rejected"
            signal_id = payload["signal"].id  # type: ignore[index]
            reason = self._reason(payload["reason"])  # type: ignore[index]
            # Forward rejection to gateway event buffer so dashboard sees it.
            sig = payload["signal"]  # type: ignore[index]
            self._send_event(event, {
                "symbol":    getattr(sig, "symbol", ""),
                "direction": self._direction(getattr(sig, "direction", None)),
                "signal_id": signal_id,
                "reason":    reason,
            })
        elif event == Events.EXECUTION_ATTEMPTED:
            stage = "attempted"
            signal_id = payload.id  # type: ignore[union-attr]
        elif event == Events.TRADE_OPENED:
            stage = "opened"
            signal_id = payload.signal_id  # type: ignore[union-attr]
            trade_id = payload.id  # type: ignore[union-attr]
            ticket = payload.entry_ticket  # type: ignore[union-attr]
            broker_ticket = str(ticket) if ticket is not None else None
            # Forward trade.opened to gateway so Activity tab receives it.
            self._send_event("trade.opened", {
                "symbol":    getattr(payload, "symbol", ""),
                "direction": self._direction(getattr(payload, "side", None)),
                "trade_id":  trade_id,
                "signal_id": signal_id,
                "ticket":    broker_ticket,
                "volume":    getattr(payload, "entry_lots", None),
                "price":     getattr(payload, "entry_price", None),
            })
        elif event == Events.TRADE_ERROR:
            stage = "failed"
            signal_id = payload["signal"].id  # type: ignore[index]
            reason = self._reason(payload["reason"])  # type: ignore[index]
            # Forward trade.error to gateway event buffer so Rejections tab sees it.
            sig = payload["signal"]  # type: ignore[index]
            self._send_event("trade.error", {
                "symbol":    getattr(sig, "symbol", ""),
                "direction": self._direction(getattr(sig, "direction", None)),
                "signal_id": signal_id,
                "reason":    reason,
                "message":   str(payload.get("message", "")),  # type: ignore[union-attr]
            })
        elif event in {
            Events.TRADE_CLOSED, Events.TRADE_TP1_HIT,
            Events.TRADE_TP2_HIT, Events.TRADE_SL_HIT,
        }:
            # No lifecycle stage for these — just forward to dashboard Activity tab.
            self._send_event(event, {
                "symbol":    getattr(payload, "symbol", ""),
                "direction": self._direction(getattr(payload, "side", None)),
                "trade_id":  getattr(payload, "id", None),
                "ticket":    getattr(payload, "entry_ticket", None),
                "volume":    getattr(payload, "current_lots", None),
                "price":     getattr(payload, "close_price", getattr(payload, "entry_price", None)),
                "pnl":       getattr(payload, "realized_pnl", None),
                "profit":    getattr(payload, "realized_pnl", None),
            })

        if stage and signal_id:
            self._queue_lifecycle(stage, signal_id, reason, trade_id, broker_ticket)

    def _send_event(self, event_type: str, data: dict) -> None:
        """
        Forward an execution event to the gateway's per-engine event buffer.

        The gateway stores each event in ``eventBuffers[engineId]`` and merges
        them into ``recent_events`` on every ``execution.metrics.snapshot``
        broadcast.  The customer dashboard reads ``recent_events`` to populate
        the Rejections, Activity, and Logs tabs.

        Silently skips if the engine is not yet activated (no WS connection).
        """
        if self._execution_event_sink:
            self._execution_event_sink(event_type, data)
            return
        if not self._activated.is_set():
            return
        self._send("execution.event", {
            "event_type": event_type,
            "data": data,
        })

    @staticmethod
    def _direction(value: object) -> str:
        """Return the string value of an enum direction/side, or '' if None."""
        if value is None:
            return ""
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    def _queue_lifecycle(
        self,
        stage: str,
        signal_id: str,
        reason: str | None = None,
        trade_id: str | None = None,
        broker_ticket: str | None = None,
    ) -> None:
        report = {
            "engine_id": self._engine_id,
            "signal_id": signal_id,
            "account_login": self._account_login,
            "stage": stage,
            "observed_at": self._utc_now(),
        }
        if reason:
            report["reason"] = reason[:1000]
        if trade_id:
            report["trade_id"] = trade_id
        if broker_ticket:
            report["broker_ticket"] = broker_ticket
        # 2.11 — Persist to outbox before enqueuing so the event survives a WS disconnect
        outbox_id = self._persist_to_outbox(
            "execution.lifecycle", report, stage=stage, signal_id=signal_id
        )

        try:
            self._lifecycle_queue.put_nowait(("execution.lifecycle", report, outbox_id))
        except queue.Full:
            logger.error(
                "Execution lifecycle queue full; report dropped",
                extra={"stage": stage, "signal_id": signal_id},
            )

    def _persist_to_outbox(
        self,
        event: str,
        report: dict,
        *,
        stage: str,
        signal_id: str,
        attempts: int = 3,
    ) -> Optional[int]:
        """
        BUG-09 — Write an event to the outbox with bounded retries so a transient
        SQLite failure (lock contention, brief I/O error) cannot leave the event
        in-memory only. Returns the row ID, or None if every attempt failed.
        """
        if self._db is None:
            return None
        payload = json.dumps(report, separators=(",", ":"))
        for attempt in range(1, attempts + 1):
            try:
                return self._db.outbox_enqueue(event, payload)
            except Exception:
                if attempt == attempts:
                    logger.exception(
                        "Outbox write failed after %d attempts; event is in-memory only",
                        attempts,
                        extra={"stage": stage, "signal_id": signal_id},
                    )
                else:
                    self._stopped.wait(timeout=0.05 * attempt)
        return None

    def _send_lifecycle_reports(self) -> None:
        while not self._stopped.is_set():
            if not self._activated.wait(timeout=1.0):
                continue
            try:
                event, report, outbox_id = self._lifecycle_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._deliver_lifecycle_report(event, report, outbox_id)
            self._lifecycle_queue.task_done()

    def _deliver_lifecycle_report(
        self, event: str, report: dict, outbox_id: Optional[int]
    ) -> bool:
        sent = self._send(event, report) is not None
        if sent:
            # 2.11 — Mark delivered in the outbox so it is not replayed on reconnect
            if outbox_id is not None and self._db is not None:
                try:
                    self._db.outbox_mark_sent(outbox_id)
                except Exception:
                    logger.exception(
                        "Outbox mark-sent failed", extra={"outbox_id": outbox_id}
                    )
        else:
            logger.warning(
                "Execution lifecycle report could not be sent",
                extra={"stage": report["stage"], "signal_id": report["signal_id"]},
            )
            # BUG-09 — Last-chance persist: if the event never reached the
            # outbox, retry the write now so the reconnect replay can recover
            # it even though the in-band send just failed.
            if outbox_id is None:
                self._persist_to_outbox(
                    event,
                    report,
                    stage=str(report.get("stage", "")),
                    signal_id=str(report.get("signal_id", "")),
                )
        return sent

    @staticmethod
    def _reason(value: object) -> str:
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)
        return str(value)

    def _send(self, event: str, payload: dict) -> str | None:
        message_id = str(uuid4())
        message = {
            "event": event,
            "data": {
                "protocol_version": "1.0",
                "message_id": message_id,
                "sent_at": self._utc_now(),
                "payload": payload,
            },
        }
        if self._ws.send(json.dumps(message, separators=(",", ":"))):
            return message_id
        return None

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _handle_raw(self, raw: str) -> None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("SignalConsumer: JSON parse error", extra={"raw": raw[:200]})
            metrics.increment("signal.parse_errors")
            return

        if not isinstance(parsed, dict):
            return

        event = parsed.get("event", "")
        data = parsed.get("data", {})
        if event and isinstance(data, dict) and self._handle_control(event, data):
            return

        payload = parsed.get("payload", {})

        if not event or not isinstance(payload, dict):
            return

        if not is_valid_signal_dict(payload):
            logger.debug(
                "SignalConsumer: payload is not a signal, skipping event=%s", event
            )
            return

        metrics.increment(f"signal.received.{event}")
        self._process(event, payload)

    def _handle_control(self, event: str, data: dict) -> bool:
        message_id = data.get("message_id")

        if event == "protocol.accepted" and message_id == self._hello_message_id:
            self._activate()
            return True

        if event == "activation.accepted":
            # Accept both normal activation.request response (activation_message_id)
            # and 1.16 fast-path response correlated to engine.hello (hello_message_id).
            valid_ids = {self._activation_message_id, self._hello_message_id}
            if message_id not in valid_ids:
                logger.warning("Ignoring unexpected Gateway activation response")
                return True
            if data.get("engine_id") != self._engine_id:
                logger.error("Gateway activation response engine_id mismatch")
                return True
            # 1.16 — Store fresh credential if the gateway issued one
            new_cred = data.get("device_credential")
            if new_cred and isinstance(new_cred, str):
                self._device_credential = new_cred
                if self._db is not None:
                    try:
                        self._db.save_device_state("device_credential", new_cred)
                        logger.info("Device credential stored for future fast-path reconnects")
                    except Exception:
                        logger.exception("Failed to persist device credential")
            self._activated.set()
            self._subscribe()
            # 2.11 — Replay any lifecycle events that were persisted but not delivered
            self._replay_outbox()
            fast_path = message_id == self._hello_message_id and self._activation_message_id is None
            logger.info(
                "Apex Quantel activation accepted",
                extra={
                    "engine_id": self._engine_id,
                    "symbols": data.get("symbols", []),
                    "fast_path": fast_path,
                    "credential_received": bool(new_cred),
                },
            )
            return True

        if event == "protocol.rejected":
            if message_id in {self._hello_message_id, self._activation_message_id}:
                self._activated.clear()
                logger.error(
                    "Apex Quantel Gateway rejected connection setup",
                    extra={"message_id": message_id, "errors": data.get("errors", [])},
                )
            return True

        if event == "execution.metrics.subscribe":
            self._metrics_subscribed.set()
            if self._snapshot_provider:
                self._send("execution.metrics.snapshot", self._snapshot_provider())
            return True

        if event == "execution.metrics.unsubscribe":
            self._metrics_subscribed.clear()
            return True

        if event in {"command.pause", "command.resume", "command.emergency_stop"}:
            self._handle_command(event, data)
            return True

        if event == "license.updated":
            self._handle_license_updated(data)
            return True

        # BUG-15: All events in this set are handled by explicit `return True`
        # branches above and can never reach this line.  The set was dead code —
        # replaced with a plain False so unknown events are forwarded to the signal
        # pipeline as intended.
        return False

    def _replay_outbox(self) -> None:
        """
        2.11 — On reconnect, re-enqueue any lifecycle events that were persisted
        to the outbox but never confirmed delivered.  Called after activation.
        """
        if self._db is None:
            return
        try:
            pending = self._db.outbox_load_pending()
        except Exception:
            logger.exception("Outbox replay: could not load pending events")
            return
        if not pending:
            return

        replayed = 0
        for row_id, ev, payload_json in pending:
            try:
                report = json.loads(payload_json)
            except json.JSONDecodeError:
                logger.warning("Outbox replay: invalid JSON in row %d — skipping", row_id)
                continue
            try:
                self._lifecycle_queue.put_nowait((ev, report, row_id))
                replayed += 1
            except queue.Full:
                logger.warning(
                    "Outbox replay: lifecycle queue full — %d event(s) not replayed",
                    len(pending) - replayed,
                )
                break

        if replayed:
            logger.info("Outbox replay: re-enqueued %d pending event(s)", replayed)

    def _handle_license_updated(self, data: dict) -> None:
        """
        2.12 — Reacts to a ``license.updated`` push from the gateway.

        Status transitions:
          - ``active``: log info — no action needed, licence was already accepted
          - ``suspended`` / ``revoked``: log a critical warning and disconnect.
            The gateway will refuse re-activation until the key is re-issued.
          - Any other status: log a warning for visibility.

        Disconnecting causes the WebSocket client to reconnect and re-run the
        activation handshake, which will fail with the new status. This is the
        correct behaviour — the engine should not continue executing trades on
        a suspended licence.
        """
        status = str(data.get("status", "")).lower()
        license_id = data.get("license_id") or data.get("id", "<unknown>")

        if status == "active":
            logger.info(
                "license.updated: licence is active — no action required",
                extra={"license_id": license_id},
            )
            return

        if status in {"suspended", "revoked", "expired"}:
            logger.critical(
                "license.updated: licence status=%s — stopping engine. "
                "Re-issue the activation key and restart to resume.",
                status,
                extra={"license_id": license_id},
            )
            # Call self.stop() (not self._ws.stop()) so the SignalConsumer-level
            # _stopped event is set.  Without it, _refresh_rooms / _heartbeat /
            # _lifecycle / _metrics threads keep spinning until process exit.
            self.stop()
            return

        logger.warning(
            "license.updated: unrecognised status=%s — no action taken",
            status,
            extra={"license_id": license_id, "data": data},
        )

    def _handle_command(self, event: str, data: dict) -> None:
        """
        Handles remote command events from the gateway.

        Each command is executed in the calling thread (the WebSocket reader),
        which is acceptable because pause/resume are non-blocking and
        emergency_stop closes positions via a brief MT5 call on a daemon thread.

        Replies ``command.completed`` on success or ``command.failed`` on error.
        """
        command_id = data.get("command_id") or data.get("data", {}).get("command_id")
        if not command_id:
            logger.warning("Received %s with no command_id — ignoring", event)
            return

        logger.info("Remote command received", extra={"event": event, "command_id": command_id})

        callback_map: dict[str, Callable[[], None] | None] = {
            "command.pause":          self._on_pause,
            "command.resume":         self._on_resume,
            "command.emergency_stop": self._on_emergency_stop,
        }
        callback = callback_map.get(event)

        if callback is None:
            reason = f"No handler registered for {event}"
            logger.error(reason, extra={"command_id": command_id})
            self._send("command.failed", {"command_id": command_id, "reason": reason})
            return

        # Execute the action in a background thread so the WS reader is not stalled
        def _run() -> None:
            try:
                callback()  # type: ignore[misc]
                logger.info(
                    "Remote command completed", extra={"event": event, "command_id": command_id}
                )
                self._send("command.completed", {"command_id": command_id, "result": {}})
            except Exception as exc:
                reason = str(exc)
                logger.exception(
                    "Remote command failed",
                    extra={"event": event, "command_id": command_id, "reason": reason},
                )
                self._send("command.failed", {"command_id": command_id, "reason": reason})

        t = threading.Thread(target=_run, name=f"cmd-{event}", daemon=True)
        t.start()

    def _process(self, event: str, payload: dict) -> None:
        # ── 2.8 — Cryptographic signature validation ───────────────────────
        if self._signal_hmac_secret is not None:
            provided_sig = payload.get("signature")
            if not provided_sig:
                logger.warning(
                    "SignalConsumer: signal rejected — signature field missing "
                    "and SIGNAL_HMAC_SECRET is configured",
                    extra={"event": event},
                )
                metrics.increment("signal.signature_missing")
                return
            # Build canonical message: serialise payload without the signature field
            body = {k: v for k, v in payload.items() if k != "signature"}
            body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            expected_sig = hmac.new(
                self._signal_hmac_secret, body_bytes, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(provided_sig, expected_sig):
                logger.warning(
                    "SignalConsumer: signal rejected — HMAC signature mismatch",
                    extra={"event": event},
                )
                metrics.increment("signal.signature_invalid")
                return

        # ── 2.10 — Duplicate (event, signal_id) deduplication ─────────────
        signal_id = payload.get("id") or payload.get("signal_id") or ""
        if signal_id:
            dedupe_key = (event, signal_id)
            if dedupe_key in self._seen_ids:
                logger.debug(
                    "SignalConsumer: duplicate event/signal_id dropped",
                    extra={"signal_id": signal_id, "event": event},
                )
                metrics.increment("signal.duplicate_dropped")
                return
            # Bounded eviction — remove oldest entry if at capacity
            if len(self._seen_ids) >= self._SEEN_IDS_MAX:
                self._seen_ids.popitem(last=False)
            self._seen_ids[dedupe_key] = None

        try:
            signal = InboundSignal.from_dict(payload)
        except Exception:
            logger.exception("SignalConsumer: failed to deserialise signal")
            metrics.increment("signal.deserialise_errors")
            return

        received_log_at = signal.received_at or now_ms()
        actionable_at = signal.setup_candle_close_at or signal.triggered_at
        logger.info(
            "Signal received",
            extra={
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "setup_candle_open_at": signal.setup_candle_open_at,
                "setup_candle_close_at": signal.setup_candle_close_at,
                "triggered_at": signal.triggered_at,
                "emitted_at": signal.emitted_at,
                "received_at": signal.received_at,
                "age_ms": received_log_at - actionable_at if actionable_at else None,
            },
        )
        if event in SIGNAL_TRIGGER_EVENTS:
            self._queue_lifecycle("received", signal.id)

        result = self._validator.validate(signal)

        if not result.valid:
            logger.warning(
                "SignalConsumer: signal rejected by validator",
                extra={"signal_id": signal.id, "errors": result.errors},
            )
            metrics.increment("signal.validation_failures")
            self._bus.emit(
                Events.SIGNAL_REJECTED, {"signal": signal, "reason": result.errors}
            )
            return

        self._bus.emit(Events.SIGNAL_RECEIVED, {"event": event, "signal": signal})

        if event in SIGNAL_TRIGGER_EVENTS:
            logger.info(
                "SignalConsumer: triggered — forwarding for execution",
                extra={
                    "signal_id": signal.id,
                    "symbol": signal.symbol,
                    "direction": signal.direction.value,
                    "setup_candle_open_at": signal.setup_candle_open_at,
                    "setup_candle_close_at": signal.setup_candle_close_at,
                    "triggered_at": signal.triggered_at,
                    "emitted_at": signal.emitted_at,
                    "received_at": signal.received_at,
                },
            )
            metrics.increment("signal.triggered")
            self._bus.emit(Events.SIGNAL_TRIGGERED, signal)

        elif event in SIGNAL_CLOSE_EVENTS:
            logger.info(
                "SignalConsumer: close event received (informational)",
                extra={"event": event, "signal_id": signal.id},
            )
            metrics.increment(f"signal.close.{event}")
