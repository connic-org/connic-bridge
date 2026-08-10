import asyncio
import json
import signal
from unittest.mock import MagicMock

import websockets.exceptions
from websockets.frames import Close

import connic_bridge.agent as agent_module
from connic_bridge.agent import BridgeAgent


CHANNEL_ID = "12345678-1234-5678-1234-567812345678"


class RelaySocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


class ScriptedRelaySocket(RelaySocket):
    def __init__(self, messages):
        super().__init__()
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class RecordingTcpService:
    def __init__(self):
        self.received = asyncio.Queue()
        self.host = "postgres.internal"
        self.port = 5432
        self._reader = None

    async def connect(self, host, port):
        assert (host, port) == (self.host, self.port)
        self._reader = QueueReader()
        return self._reader, RecordingWriter(self)

    async def close(self):
        if self._reader:
            await self._reader.feed_eof()


class IdleTcpService:
    def __init__(self):
        self.host = "postgres.internal"
        self.port = 5432
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self._reader = None

    async def connect(self, host, port):
        assert (host, port) == (self.host, self.port)
        self.connected.set()
        self._reader = QueueReader()
        return self._reader, IdleWriter(self)

    async def close(self):
        if self._reader:
            await self._reader.feed_eof()


class QueueReader:
    def __init__(self):
        self._chunks = asyncio.Queue()

    async def read(self, _size):
        return await self._chunks.get()

    async def feed_data(self, data):
        await self._chunks.put(data)

    async def feed_eof(self):
        await self._chunks.put(b"")


class RecordingWriter:
    def __init__(self, service):
        self.service = service
        self._buffer = bytearray()
        self.closed = False

    def write(self, data):
        self._buffer.extend(data)

    async def drain(self):
        data = bytes(self._buffer)
        self._buffer.clear()
        await self.service.received.put(data)
        await self.service._reader.feed_data(b"pong")
        await self.service._reader.feed_eof()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class IdleWriter:
    def __init__(self, service):
        self.service = service
        self.closed = False

    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True
        self.service.closed.set()

    async def wait_closed(self):
        await self.service._reader.feed_eof()


def decode_control(message):
    return json.loads(message)


def test_rejects_connect_request_when_target_is_not_allowed():
    async def scenario():
        relay = RelaySocket()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        agent._ws = relay

        await agent._handle_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": "kafka",
            "port": 9092,
        })

        assert agent._channels == {}
        assert len(relay.sent) == 1
        assert decode_control(relay.sent[0]) == {
            "type": "error",
            "channel": CHANNEL_ID,
            "message": "Host kafka:9092 is not in the allowed hosts list",
        }

    asyncio.run(scenario())


def test_open_channel_proxies_data_between_relay_and_allowed_tcp_target(monkeypatch):
    async def scenario():
        service = RecordingTcpService()
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        try:
            relay = RelaySocket()
            agent = BridgeAgent(
                "wss://relay.example",
                "cbr_test",
                {f"{service.host}:{service.port}"},
            )
            agent._ws = relay

            await agent._handle_control({
                "type": "connect",
                "channel": CHANNEL_ID,
                "host": service.host,
                "port": service.port,
            })

            assert decode_control(relay.sent[0]) == {
                "type": "connected",
                "channel": CHANNEL_ID,
            }
            assert CHANNEL_ID in agent._channels

            await agent._handle_data(CHANNEL_ID, b"hello")

            assert await asyncio.wait_for(service.received.get(), timeout=1) == b"hello"

            tcp_to_relay_frame = await wait_for_binary_frame(relay)
            assert tcp_to_relay_frame == CHANNEL_ID.encode("utf-8") + b"pong"

            close_message = await wait_for_control(relay, "close")
            assert close_message == {"type": "close", "channel": CHANNEL_ID}
            assert CHANNEL_ID not in agent._channels
        finally:
            await agent.stop()
            await service.close()

    asyncio.run(scenario())


def test_allowed_target_connection_failure_is_reported_to_relay(monkeypatch):
    async def scenario():
        host, port = "postgres.internal", 5432

        async def fail_to_connect(_host, _port):
            raise OSError("connection refused")

        monkeypatch.setattr(agent_module.asyncio, "open_connection", fail_to_connect)
        relay = RelaySocket()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {f"{host}:{port}"})
        agent._ws = relay

        await agent._handle_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": host,
            "port": port,
        })

        assert agent._channels == {}
        assert len(relay.sent) == 1
        control = decode_control(relay.sent[0])
        assert control["type"] == "error"
        assert control["channel"] == CHANNEL_ID
        assert control["message"].startswith(f"Failed to connect to {host}:{port}:")

    asyncio.run(scenario())


def test_allowed_target_connection_timeout_is_reported_to_relay(monkeypatch):
    async def scenario():
        relay = RelaySocket()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"kafka.internal:9092"})
        agent._ws = relay
        real_wait_for = asyncio.wait_for

        async def never_finishes():
            await asyncio.Event().wait()

        def fake_open_connection(host, port):
            assert (host, port) == ("kafka.internal", 9092)
            return never_finishes()

        async def short_wait_for(awaitable, timeout):
            assert timeout == 10.0
            task = asyncio.create_task(awaitable)
            try:
                await real_wait_for(task, timeout=0.01)
            except asyncio.TimeoutError:
                raise

        monkeypatch.setattr(agent_module.asyncio, "open_connection", fake_open_connection)
        monkeypatch.setattr(agent_module.asyncio, "wait_for", short_wait_for)

        await agent._handle_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": "kafka.internal",
            "port": 9092,
        })

        assert agent._channels == {}
        assert len(relay.sent) == 1
        assert decode_control(relay.sent[0]) == {
            "type": "error",
            "channel": CHANNEL_ID,
            "message": "Connection to kafka.internal:9092 timed out",
        }

    asyncio.run(scenario())


def test_relay_data_to_failed_tcp_writer_closes_channel_and_notifies_relay():
    async def scenario():
        class FailingWriter:
            def __init__(self):
                self.writes = []
                self.closed = False

            def write(self, data):
                self.writes.append(data)

            async def drain(self):
                raise ConnectionError("local target disconnected")

            def close(self):
                self.closed = True

            async def wait_closed(self):
                raise RuntimeError("already gone")

        relay = RelaySocket()
        writer = FailingWriter()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"kafka.internal:9092"})
        agent._ws = relay
        agent._channels[CHANNEL_ID] = (object(), writer)

        await agent._handle_data("missing-channel", b"ignored")
        await agent._handle_data(CHANNEL_ID, b"hello")

        assert writer.writes == [b"hello"]
        assert writer.closed is True
        assert CHANNEL_ID not in agent._channels
        assert decode_control(relay.sent[-1]) == {"type": "close", "channel": CHANNEL_ID}

    asyncio.run(scenario())


def test_stop_closes_active_tcp_channels_and_relay_socket():
    async def scenario():
        class ClosingWriter:
            def __init__(self):
                self.closed = False
                self.waited = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                self.waited = True

        relay = RelaySocket()
        writer = ClosingWriter()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres.internal:5432"})
        agent._ws = relay
        agent._channels[CHANNEL_ID] = (object(), writer)

        await agent.stop()

        assert agent._running is False
        assert writer.closed is True
        assert writer.waited is True
        assert relay.closed is True
        assert CHANNEL_ID not in agent._channels
        assert decode_control(relay.sent[-1]) == {"type": "close", "channel": CHANNEL_ID}

    asyncio.run(scenario())


def test_relay_close_request_closes_tcp_channel_and_notifies_relay(monkeypatch):
    async def scenario():
        service = IdleTcpService()
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        try:
            relay = RelaySocket()
            agent = BridgeAgent(
                "wss://relay.example",
                "cbr_test",
                {f"{service.host}:{service.port}"},
            )
            agent._ws = relay

            await agent._handle_control({
                "type": "connect",
                "channel": CHANNEL_ID,
                "host": service.host,
                "port": service.port,
            })
            await asyncio.wait_for(service.connected.wait(), timeout=1)
            forwarder = agent._channel_tasks[CHANNEL_ID]

            await agent._handle_control({"type": "close", "channel": CHANNEL_ID})
            await forwarder

            await asyncio.wait_for(service.closed.wait(), timeout=1)
            assert CHANNEL_ID not in agent._channels
            assert CHANNEL_ID not in agent._channel_tasks
            assert [decode_control(message) for message in relay.sent] == [
                {"type": "connected", "channel": CHANNEL_ID},
                {"type": "close", "channel": CHANNEL_ID},
            ]
        finally:
            await agent.stop()
            await service.close()

    asyncio.run(scenario())


def test_connect_and_serve_processes_relay_control_and_data_frames(monkeypatch):
    async def scenario():
        service = RecordingTcpService()
        agent = None
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        try:
            relay = ScriptedRelaySocket([
                json.dumps({"type": "welcome", "bridge_id": "bridge_test"}),
                "not-json",
                b"short",
                json.dumps({
                    "type": "connect",
                    "channel": CHANNEL_ID,
                    "host": service.host,
                    "port": service.port,
                }),
                CHANNEL_ID.encode("utf-8") + b"hello",
            ])
            connect_calls = []

            def fake_connect(url, **kwargs):
                connect_calls.append((url, kwargs))
                return relay

            monkeypatch.setattr(agent_module.websockets, "connect", fake_connect)
            agent = BridgeAgent(
                "wss://relay.example/tunnel",
                "cbr_test",
                {f"{service.host}:{service.port}"},
            )

            await agent._connect_and_serve()

            assert connect_calls == [
                (
                    "wss://relay.example/tunnel?token=cbr_test",
                    {"ping_interval": 30, "ping_timeout": 10},
                )
            ]
            assert decode_control(relay.sent[0]) == {"type": "connected", "channel": CHANNEL_ID}
            assert await asyncio.wait_for(service.received.get(), timeout=1) == b"hello"
            assert await wait_for_binary_frame(relay) == CHANNEL_ID.encode("utf-8") + b"pong"
        finally:
            if agent:
                await agent.stop()
            await service.close()

    asyncio.run(scenario())


def test_connect_and_serve_preserves_relay_url_query_parameters(monkeypatch):
    async def scenario():
        relay = ScriptedRelaySocket([])
        connect_calls = []

        def fake_connect(url, **kwargs):
            connect_calls.append((url, kwargs))
            return relay

        monkeypatch.setattr(agent_module.websockets, "connect", fake_connect)
        agent = BridgeAgent(
            "wss://relay.example/tunnel?region=eu",
            "cbr_test",
            {"postgres:5432"},
        )

        await agent._connect_and_serve()

        assert connect_calls == [
            (
                "wss://relay.example/tunnel?region=eu&token=cbr_test",
                {"ping_interval": 30, "ping_timeout": 10},
            )
        ]

    asyncio.run(scenario())


def test_connect_and_serve_closes_idle_tcp_channel_when_relay_disconnects(monkeypatch):
    async def scenario():
        service = IdleTcpService()
        agent = None
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        try:
            relay = ScriptedRelaySocket([
                json.dumps({
                    "type": "connect",
                    "channel": CHANNEL_ID,
                    "host": service.host,
                    "port": service.port,
                }),
            ])
            monkeypatch.setattr(agent_module.websockets, "connect", lambda *_args, **_kwargs: relay)
            agent = BridgeAgent(
                "wss://relay.example/tunnel",
                "cbr_test",
                {f"{service.host}:{service.port}"},
            )

            await agent._connect_and_serve()

            assert service.closed.is_set()
            assert agent._channels == {}
            assert agent._channel_tasks == {}
            assert agent._ws is None
        finally:
            if agent:
                await agent.stop()
            await service.close()

    asyncio.run(scenario())


async def wait_for_binary_frame(relay):
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        for message in relay.sent:
            if isinstance(message, bytes):
                return message
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for TCP data frame")


async def wait_for_control(relay, message_type):
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        for message in relay.sent:
            if not isinstance(message, str):
                continue
            control = decode_control(message)
            if control.get("type") == message_type:
                return control
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {message_type} control message")


# ---------------------------------------------------------------------------
# run() reconnection loop tests
# ---------------------------------------------------------------------------


def test_run_stops_immediately_on_auth_failure_403(monkeypatch):
    """When relay returns 403, run() logs auth failure and returns without retrying."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            # Simulate InvalidStatusCode with status 403
            exc = websockets.exceptions.InvalidStatusCode(403, None)
            raise exc

        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_bad_token", {"postgres:5432"})
        await agent.run()

        # Should have called _connect_and_serve exactly once (no retry)
        assert call_count == 1

    asyncio.run(scenario())


def test_run_stops_immediately_on_auth_failure_401(monkeypatch):
    """When relay returns 401, run() logs auth failure and returns without retrying."""
    async def scenario():
        call_count = 0

        class FakeResponse:
            status_code = 401

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            # websockets >=13 uses InvalidStatus with response attribute
            exc = websockets.exceptions.InvalidStatusCode(401, None)
            raise exc

        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_bad_token", {"postgres:5432"})
        await agent.run()

        assert call_count == 1

    asyncio.run(scenario())


def test_run_stops_immediately_when_relay_revokes_active_token(monkeypatch):
    async def scenario():
        call_count = 0
        reconnect_delays = []

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                close = Close(4003, "Bridge token revoked")
                raise websockets.exceptions.ConnectionClosedError(close, close, True)
            self._running = False

        async def record_sleep(seconds):
            reconnect_delays.append(seconds)

        monkeypatch.setattr(agent_module.asyncio, "sleep", record_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_revoked", {"postgres:5432"})

        await agent.run()

        assert call_count == 1
        assert reconnect_delays == []

    asyncio.run(scenario())


def test_run_retries_on_connection_error_then_stops(monkeypatch):
    """ConnectionError triggers reconnection. Second call stops the agent."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network unreachable")
            # On second call, stop the agent to exit the loop
            self._running = False

        # Monkeypatch sleep to avoid real delays
        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(agent_module.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        await agent.run()

        assert call_count == 2
        assert len(sleeps) == 1
        assert sleeps[0] == 2  # base_delay

    asyncio.run(scenario())


def test_stop_interrupts_maximum_reconnect_backoff(monkeypatch):
    async def scenario():
        backoff_started = asyncio.Event()
        backoff_cancelled = asyncio.Event()
        delays = []

        async def fail_to_connect(self):
            raise ConnectionError("relay unavailable")

        async def controlled_sleep(seconds):
            delays.append(seconds)
            if seconds < 60:
                return
            backoff_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                backoff_cancelled.set()
                raise

        monkeypatch.setattr(agent_module.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fail_to_connect)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        run_task = asyncio.create_task(agent.run())

        await asyncio.wait_for(backoff_started.wait(), timeout=1)
        await agent.stop()
        await asyncio.wait_for(run_task, timeout=0.1)

        assert delays == [2, 4, 8, 16, 32, 60]
        assert backoff_cancelled.is_set()

    asyncio.run(scenario())


def test_cancelling_run_during_reconnect_backoff_propagates(monkeypatch):
    async def scenario():
        backoff_started = asyncio.Event()

        async def fail_to_connect(self):
            raise ConnectionError("relay unavailable")

        async def controlled_sleep(seconds):
            assert seconds == 2
            backoff_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(agent_module.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fail_to_connect)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        run_task = asyncio.create_task(agent.run())

        await asyncio.wait_for(backoff_started.wait(), timeout=1)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

        assert run_task.cancelled()

    asyncio.run(scenario())


def test_run_retries_on_unexpected_error_with_exponential_backoff(monkeypatch):
    """Unexpected errors trigger retry with exponential backoff."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise RuntimeError(f"unexpected error #{call_count}")
            self._running = False

        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(agent_module.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        await agent.run()

        assert call_count == 4
        # Exponential backoff: 2, 4, 8
        assert sleeps == [2, 4, 8]

    asyncio.run(scenario())


def test_run_resets_delay_after_clean_disconnect(monkeypatch):
    """After a clean disconnect, reconnection delay resets to base."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("first failure")
            if call_count == 2:
                # Clean return (no exception) = clean disconnect
                return
            if call_count == 3:
                raise ConnectionError("second failure after clean disconnect")
            self._running = False

        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(agent_module.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        await agent.run()

        assert call_count == 4
        # Call 1: ConnectionError → sleep(2), delay doubles to 4
        # Call 2: clean return → delay resets to 2, sleep(2), delay doubles to 4
        # Call 3: ConnectionError → sleep(4), delay doubles to 8
        # Without the reset after call 2, call 3 would have slept 8 instead of 4
        assert sleeps == [2, 2, 4]

    asyncio.run(scenario())


def test_run_retries_on_rejected_connection_non_auth_status(monkeypatch):
    """Non-auth InvalidStatusCode (e.g. 503) triggers retry, not exit."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise websockets.exceptions.InvalidStatusCode(503, None)
            self._running = False

        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(agent_module.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        await agent.run()

        assert call_count == 2
        assert sleeps == [2]

    asyncio.run(scenario())


def test_run_retries_on_connection_closed(monkeypatch):
    """A non-auth WebSocket close triggers reconnection."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                close = Close(1011, "Relay unavailable")
                raise websockets.exceptions.ConnectionClosedError(close, close, True)
            if call_count == 2:
                raise websockets.exceptions.ConnectionClosed(None, None)
            self._running = False

        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(agent_module.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        await agent.run()

        assert call_count == 3
        assert sleeps == [2, 4]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# _forward_tcp_to_relay – error handling
# ---------------------------------------------------------------------------


def test_forward_tcp_to_relay_handles_connection_error():
    """When the local TCP reader raises ConnectionError, the channel is closed cleanly."""
    async def scenario():
        relay = RelaySocket()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        agent._ws = relay

        class ErrorReader:
            async def read(self, n):
                raise ConnectionError("local target gone")

        class DummyWriter:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
            async def wait_closed(self):
                pass

        writer = DummyWriter()
        agent._channels[CHANNEL_ID] = (ErrorReader(), writer)

        await agent._forward_tcp_to_relay(CHANNEL_ID, ErrorReader())

        # Channel should have been cleaned up
        assert CHANNEL_ID not in agent._channels
        assert writer.closed is True

    asyncio.run(scenario())


def test_forward_tcp_to_relay_handles_eof():
    """When reader returns empty bytes (EOF), forwarding stops and channel is closed."""
    async def scenario():
        relay = RelaySocket()
        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        agent._ws = relay

        class EofReader:
            async def read(self, n):
                return b""  # EOF

        class DummyWriter:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
            async def wait_closed(self):
                pass

        writer = DummyWriter()
        agent._channels[CHANNEL_ID] = (EofReader(), writer)

        await agent._forward_tcp_to_relay(CHANNEL_ID, EofReader())

        assert CHANNEL_ID not in agent._channels

    asyncio.run(scenario())


def test_forward_tcp_to_relay_drops_late_data_after_relay_disconnect():
    async def scenario():
        service = IdleTcpService()
        reader, writer = await service.connect(service.host, service.port)
        await reader.feed_data(b"late response")
        await reader.feed_eof()

        agent = BridgeAgent(
            "wss://relay.example",
            "cbr_test",
            {f"{service.host}:{service.port}"},
        )
        agent._channels[CHANNEL_ID] = (reader, writer)

        await agent._forward_tcp_to_relay(CHANNEL_ID, reader)

        assert service.closed.is_set()
        assert agent._channels == {}

    asyncio.run(scenario())


def test_tcp_eof_notifies_relay_without_cancelling_forwarder(monkeypatch):
    async def scenario():
        class YieldingRelaySocket(RelaySocket):
            async def send(self, message):
                await asyncio.sleep(0)
                await super().send(message)

        service = IdleTcpService()
        relay = YieldingRelaySocket()
        agent = BridgeAgent(
            "wss://relay.example",
            "cbr_test",
            {f"{service.host}:{service.port}"},
        )
        agent._ws = relay
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        try:
            await agent._handle_control({
                "type": "connect",
                "channel": CHANNEL_ID,
                "host": service.host,
                "port": service.port,
            })
            forwarder = agent._channel_tasks[CHANNEL_ID]

            await service._reader.feed_eof()
            await forwarder

            assert forwarder.cancelled() is False
            assert service.closed.is_set()
            assert agent._channels == {}
            assert agent._channel_tasks == {}
            assert [decode_control(message) for message in relay.sent] == [
                {"type": "connected", "channel": CHANNEL_ID},
                {"type": "close", "channel": CHANNEL_ID},
            ]
        finally:
            await agent.stop()
            await service.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# _close_channel – send_control error handling
# ---------------------------------------------------------------------------


def test_close_channel_handles_send_control_failure():
    """When _send_control fails during close, it should not raise."""
    async def scenario():
        class BrokenRelaySocket:
            async def send(self, message):
                raise ConnectionError("relay gone")
            async def close(self):
                pass

        class DummyWriter:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
            async def wait_closed(self):
                pass

        agent = BridgeAgent("wss://relay.example", "cbr_test", {"postgres:5432"})
        agent._ws = BrokenRelaySocket()
        writer = DummyWriter()
        agent._channels[CHANNEL_ID] = (object(), writer)

        # Should not raise even though sending the close control message fails
        await agent._close_channel(CHANNEL_ID)

        assert CHANNEL_ID not in agent._channels
        assert writer.closed is True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# run_agent signal handling
# ---------------------------------------------------------------------------


def test_run_agent_function(monkeypatch):
    """run_agent sets up signal handlers and runs the agent."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            self._running = False

        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)

        from connic_bridge.agent import run_agent
        await run_agent("wss://relay.example", "cbr_test", {"postgres:5432"})

        assert call_count == 1

    asyncio.run(scenario())


def test_run_agent_registers_sigterm_and_sigint_handlers(monkeypatch):
    """Unix-style loop registers SIGTERM and SIGINT before running the agent."""
    registered = []

    def capture(sig, _callback):
        registered.append(sig)

    mock_loop = MagicMock()
    mock_loop.add_signal_handler = capture
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: mock_loop)

    async def instant_run(self):
        self._running = False

    monkeypatch.setattr(BridgeAgent, "run", instant_run)

    asyncio.run(agent_module.run_agent("wss://relay.example", "cbr_test", {"postgres:5432"}))

    assert registered == [signal.SIGTERM, signal.SIGINT]


def test_run_agent_shutdown_callback_schedules_stop(monkeypatch):
    """When the OS signal handler fires, stop() runs on the loop (graceful shutdown path)."""
    handlers = {}

    def add_handler(sig, callback):
        handlers[sig] = callback

    mock_loop = MagicMock()
    mock_loop.add_signal_handler = add_handler
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: mock_loop)

    stop_events = []

    async def fake_stop(self):
        stop_events.append("stop")
        self._running = False

    async def fake_run(self):
        handlers[signal.SIGTERM]()
        await asyncio.sleep(0)
        self._running = False

    monkeypatch.setattr(BridgeAgent, "run", fake_run)
    monkeypatch.setattr(BridgeAgent, "stop", fake_stop)

    asyncio.run(agent_module.run_agent("wss://relay.example", "cbr_test", {"postgres:5432"}))

    assert stop_events == ["stop"]


def test_run_agent_tolerates_platform_without_signal_handlers(monkeypatch):
    """When add_signal_handler is unsupported (e.g. Windows loop), startup still succeeds."""
    mock_loop = MagicMock()
    mock_loop.add_signal_handler = MagicMock(side_effect=NotImplementedError())

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: mock_loop)

    async def instant_run(self):
        self._running = False

    monkeypatch.setattr(BridgeAgent, "run", instant_run)

    asyncio.run(agent_module.run_agent("wss://relay.example", "cbr_test", {"postgres:5432"}))

    assert mock_loop.add_signal_handler.call_count == 2
