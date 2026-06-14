import json

from src.core.event_bus import EventBus
from src.signals.consumer import SignalConsumer
from src.signals.signal_validator import SignalValidator


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, data: str) -> bool:
        self.sent.append(data)
        return True


def make_consumer() -> tuple[SignalConsumer, FakeWebSocket]:
    consumer = SignalConsumer(
        event_bus=EventBus(),
        validator=SignalValidator(),
        ws_url="ws://localhost:4000/engine",
        activation_key="test-activation-key-001",
        symbols=["XAUUSD"],
        engine_id="execution-test-001",
        engine_version="0.1.0",
        room_ttl_seconds=60,
        account_login="106189638",
    )
    socket = FakeWebSocket()
    consumer._ws = socket  # type: ignore[assignment]
    return consumer, socket


def control(event: str, data: dict) -> str:
    return json.dumps({"event": event, "data": data})


def test_connected_waits_for_activation_before_joining_symbol_rooms() -> None:
    consumer, socket = make_consumer()

    consumer._on_connected()

    messages = [json.loads(raw) for raw in socket.sent]
    assert [message["event"] for message in messages] == ["engine.hello"]
    assert messages[0]["data"]["payload"]["engine_id"] == "execution-test-001"

    consumer._handle_raw(
        control("protocol.accepted", {"message_id": consumer._hello_message_id})
    )
    activation = json.loads(socket.sent[1])
    assert activation["event"] == "activation.request"
    assert activation["data"]["payload"]["activation_key"] == "test-activation-key-001"

    consumer._handle_raw(
        control(
            "activation.accepted",
            {
                "message_id": consumer._activation_message_id,
                "engine_id": "execution-test-001",
                "symbols": ["XAUUSD"],
            },
        )
    )
    subscription = json.loads(socket.sent[2])
    assert subscription["event"] == "room.subscribe"
    assert subscription["data"]["payload"] == {
        "engine_id": "execution-test-001",
        "symbols": ["XAUUSD"],
        "ttl_seconds": 60,
    }


def test_activation_rejection_does_not_join_rooms() -> None:
    consumer, socket = make_consumer()
    consumer._on_connected()
    consumer._handle_raw(
        control("protocol.accepted", {"message_id": consumer._hello_message_id})
    )

    consumer._handle_raw(
        control(
            "protocol.rejected",
            {
                "message_id": consumer._activation_message_id,
                "errors": ["invalid activation key"],
            },
        )
    )

    assert [json.loads(raw)["event"] for raw in socket.sent] == [
        "engine.hello",
        "activation.request",
    ]
    assert not consumer._activated.is_set()


def test_room_refresh_resends_subscription() -> None:
    consumer, socket = make_consumer()

    consumer._subscribe()
    consumer._subscribe()

    messages = [json.loads(raw) for raw in socket.sent]
    assert [message["event"] for message in messages] == [
        "room.subscribe",
        "room.subscribe",
    ]
    assert messages[0]["data"]["message_id"] != messages[1]["data"]["message_id"]


def test_heartbeat_increments_sequence() -> None:
    consumer, socket = make_consumer()

    consumer._heartbeat()
    consumer._heartbeat()

    messages = [json.loads(raw) for raw in socket.sent]
    assert [message["event"] for message in messages] == [
        "engine.heartbeat",
        "engine.heartbeat",
    ]
    assert [message["data"]["payload"]["sequence"] for message in messages] == [1, 2]


def test_lifecycle_report_contains_only_execution_reference_data() -> None:
    consumer, _ = make_consumer()

    consumer._queue_lifecycle("attempted", "signal-test-001")

    event, report, _outbox_id = consumer._lifecycle_queue.get_nowait()
    assert event == "execution.lifecycle"
    assert report["stage"] == "attempted"
    assert report["signal_id"] == "signal-test-001"
    assert report["account_login"] == "106189638"
    assert "symbol" not in report
    assert "signal" not in report


class FlakyOutboxDb:
    """outbox_enqueue fails N times, then succeeds. Only the outbox API is used."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.rows: list[tuple[str, str]] = []

    def load_device_state(self, key: str) -> None:
        return None

    def outbox_enqueue(self, event: str, payload_json: str) -> int:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("sqlite locked")
        self.rows.append((event, payload_json))
        return len(self.rows)


def make_consumer_with_db(db) -> tuple[SignalConsumer, FakeWebSocket]:
    consumer = SignalConsumer(
        event_bus=EventBus(),
        validator=SignalValidator(),
        ws_url="ws://localhost:4000/engine",
        activation_key="test-activation-key-001",
        symbols=["XAUUSD"],
        engine_id="execution-test-001",
        engine_version="0.1.0",
        room_ttl_seconds=60,
        account_login="106189638",
        db=db,
    )
    socket = FakeWebSocket()
    consumer._ws = socket  # type: ignore[assignment]
    return consumer, socket


def test_outbox_write_retries_transient_failure() -> None:
    """BUG-09: a transient SQLite failure must not leave the event in-memory only."""
    db = FlakyOutboxDb(failures=2)
    consumer, _ = make_consumer_with_db(db)

    consumer._queue_lifecycle("attempted", "signal-test-002")

    _event, _report, outbox_id = consumer._lifecycle_queue.get_nowait()
    assert outbox_id == 1
    assert db.calls == 3
    assert db.rows[0][0] == "execution.lifecycle"


def test_outbox_write_gives_up_after_three_attempts() -> None:
    db = FlakyOutboxDb(failures=99)
    consumer, _ = make_consumer_with_db(db)

    consumer._queue_lifecycle("attempted", "signal-test-003")

    _event, _report, outbox_id = consumer._lifecycle_queue.get_nowait()
    assert outbox_id is None
    assert db.calls == 3


def test_failed_send_persists_event_as_last_chance() -> None:
    """BUG-09: if the WS send fails and the event never reached the outbox,
    it must be persisted then so reconnect replay can recover it."""
    db = FlakyOutboxDb(failures=3)  # exhausts the 3 enqueue-time attempts
    consumer, socket = make_consumer_with_db(db)
    socket.send = lambda data: False  # type: ignore[method-assign]

    consumer._queue_lifecycle("attempted", "signal-test-004")
    event, report, outbox_id = consumer._lifecycle_queue.get_nowait()
    assert outbox_id is None

    sent = consumer._deliver_lifecycle_report(event, report, outbox_id)

    assert sent is False
    assert len(db.rows) == 1
    assert db.rows[0][0] == "execution.lifecycle"
