import asyncio
import json
import signal
import ssl
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import websockets.exceptions
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.frames import Close
from websockets.http11 import Response

import connic_bridge.agent as agent_module
from connic_bridge.agent import BridgeAgent


CHANNEL_ID = "12345678-1234-5678-1234-567812345678"
TLS_CERTIFICATE = Path(__file__).parent / "fixtures" / "localhost-cert.pem"
TLS_PRIVATE_KEY = Path(__file__).parent / "fixtures" / "localhost-key.pem"


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


class PendingTcpService:
    def __init__(self, host="unreachable.internal", port=5432):
        self.host = host
        self.port = port
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def connect(self, host, port):
        assert (host, port) == (self.host, self.port)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


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


def test_bridge_agent_rejects_unencrypted_relay_url():
    with pytest.raises(ValueError, match="Relay URL must use wss://"):
        BridgeAgent("ws://relay.example", "cbr_test", {"postgres:5432"})


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


def test_stop_during_relay_handshake_closes_connection_without_receiving(monkeypatch):
    async def scenario():
        handshake_started = asyncio.Event()
        finish_handshake = asyncio.Event()
        receive_started = asyncio.Event()

        class DelayedRelaySocket(RelaySocket):
            async def __aenter__(self):
                handshake_started.set()
                await finish_handshake.wait()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                await self.close()
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                receive_started.set()
                await asyncio.Event().wait()

        relay = DelayedRelaySocket()
        monkeypatch.setattr(
            agent_module.websockets,
            "connect",
            lambda *_args, **_kwargs: relay,
        )
        agent = BridgeAgent("wss://relay.example", "cbr_test", set())
        running = asyncio.create_task(agent.run())

        await asyncio.wait_for(handshake_started.wait(), timeout=1)
        await asyncio.wait_for(agent.stop(), timeout=1)
        finish_handshake.set()
        await asyncio.wait_for(running, timeout=1)

        assert receive_started.is_set() is False
        assert relay.closed is True
        assert agent._ws is None
        assert agent._running is False

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
            forwarder_result = (await asyncio.gather(forwarder, return_exceptions=True))[0]
            assert forwarder_result is None or isinstance(
                forwarder_result,
                asyncio.CancelledError,
            )

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
        class RelayFollowingConnectHandshake(ScriptedRelaySocket):
            async def __anext__(self):
                message = await super().__anext__()
                if isinstance(message, bytes) and len(message) >= 36:
                    await wait_for_control(self, "connected")
                return message

        service = RecordingTcpService()
        agent = None
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        try:
            relay = RelayFollowingConnectHandshake([
                json.dumps({"type": "welcome", "bridge_id": "bridge_test"}),
                json.dumps({"type": "heartbeat"}),
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


def test_slow_connect_does_not_block_data_and_relay_close_cancels_it(monkeypatch):
    async def scenario():
        existing_channel_id = "87654321-4321-8765-4321-876543218765"
        pending_service = PendingTcpService()

        class CloseAfterDataRelay(ScriptedRelaySocket):
            async def __anext__(self):
                if not self._messages:
                    assert pending_service.cancelled.is_set()
                return await super().__anext__()

        class ExistingWriter:
            def __init__(self):
                self.received = []
                self.closed = False

            def write(self, data):
                self.received.append(data)

            async def drain(self):
                pass

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        relay = CloseAfterDataRelay([
            json.dumps({
                "type": "connect",
                "channel": CHANNEL_ID,
                "host": pending_service.host,
                "port": pending_service.port,
            }),
            existing_channel_id.encode("utf-8") + b"healthcheck",
            json.dumps({"type": "close", "channel": CHANNEL_ID}),
        ])
        writer = ExistingWriter()
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{pending_service.host}:{pending_service.port}"},
        )
        agent._channels[existing_channel_id] = (object(), writer)
        monkeypatch.setattr(
            agent_module.asyncio,
            "open_connection",
            pending_service.connect,
        )
        monkeypatch.setattr(
            agent_module.websockets,
            "connect",
            lambda *_args, **_kwargs: relay,
        )

        await asyncio.wait_for(agent._connect_and_serve(), timeout=1)

        assert pending_service.started.is_set()
        assert pending_service.cancelled.is_set()
        assert writer.received == [b"healthcheck"]
        assert writer.closed is True
        assert agent._pending_connect_tasks == {}

    asyncio.run(scenario())


def test_repeated_connect_replaces_in_flight_attempt_and_close_cancels_replacement(
    monkeypatch,
):
    async def scenario():
        first = PendingTcpService("postgres-primary.internal", 5432)
        replacement = PendingTcpService("postgres-replica.internal", 5432)
        services = {
            (first.host, first.port): first,
            (replacement.host, replacement.port): replacement,
        }

        async def connect(host, port):
            return await services[(host, port)].connect(host, port)

        relay = RelaySocket()
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{first.host}:{first.port}", f"{replacement.host}:{replacement.port}"},
        )
        agent._ws = relay
        monkeypatch.setattr(agent_module.asyncio, "open_connection", connect)

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": first.host,
            "port": first.port,
        })
        first_task = agent._pending_connect_tasks[CHANNEL_ID]
        await asyncio.wait_for(first.started.wait(), timeout=1)

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": replacement.host,
            "port": replacement.port,
        })
        replacement_task = agent._pending_connect_tasks[CHANNEL_ID]
        await asyncio.wait_for(replacement.started.wait(), timeout=1)

        assert first.cancelled.is_set()
        assert first_task.cancelled()
        assert replacement_task is not first_task

        await agent._dispatch_control({"type": "close", "channel": CHANNEL_ID})

        assert replacement.cancelled.is_set()
        assert replacement_task.cancelled()
        assert agent._pending_connect_tasks == {}
        assert agent._channels == {}
        assert agent._channel_tasks == {}

    asyncio.run(scenario())


def test_stop_during_repeated_connect_does_not_start_replacement_target(monkeypatch):
    async def scenario():
        first = PendingTcpService("postgres-primary.internal", 5432)
        replacement = PendingTcpService("postgres-replica.internal", 5432)
        services = {
            (first.host, first.port): first,
            (replacement.host, replacement.port): replacement,
        }
        connect_calls = []

        async def connect(host, port):
            connect_calls.append((host, port))
            return await services[(host, port)].connect(host, port)

        relay = RelaySocket()
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{first.host}:{first.port}", f"{replacement.host}:{replacement.port}"},
        )
        agent._ws = relay
        monkeypatch.setattr(agent_module.asyncio, "open_connection", connect)

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": first.host,
            "port": first.port,
        })
        await asyncio.wait_for(first.started.wait(), timeout=1)

        cancelled_before_replacement = asyncio.Event()
        resume_replacement = asyncio.Event()
        cancel_pending_connects = agent._cancel_pending_connects

        async def cancel_then_pause(channel_id=None):
            await cancel_pending_connects(channel_id)
            if channel_id == CHANNEL_ID:
                cancelled_before_replacement.set()
                await resume_replacement.wait()

        monkeypatch.setattr(agent, "_cancel_pending_connects", cancel_then_pause)
        repeated_connect = asyncio.create_task(agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": replacement.host,
            "port": replacement.port,
        }))
        await asyncio.wait_for(cancelled_before_replacement.wait(), timeout=1)

        await asyncio.wait_for(agent.stop(), timeout=1)
        resume_replacement.set()
        await asyncio.wait_for(repeated_connect, timeout=1)
        await asyncio.sleep(0)

        assert first.cancelled.is_set()
        assert replacement.started.is_set() is False
        assert connect_calls == [(first.host, first.port)]
        assert relay.closed is True
        assert agent._pending_connect_tasks == {}

    asyncio.run(scenario())


def test_repeated_connect_for_active_channel_fails_closed_and_allows_safe_reuse(
    monkeypatch,
):
    async def scenario():
        first = IdleTcpService()
        first.host = "postgres-primary.internal"
        replacement = IdleTcpService()
        replacement.host = "postgres-replica.internal"
        services = {
            (first.host, first.port): first,
            (replacement.host, replacement.port): replacement,
        }
        connect_calls = []

        async def connect(host, port):
            connect_calls.append((host, port))
            return await services[(host, port)].connect(host, port)

        relay = RelaySocket()
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{first.host}:{first.port}", f"{replacement.host}:{replacement.port}"},
        )
        agent._ws = relay
        monkeypatch.setattr(agent_module.asyncio, "open_connection", connect)

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": first.host,
            "port": first.port,
        })
        await asyncio.wait_for(first.connected.wait(), timeout=1)
        await wait_for_control(relay, "connected")
        first_forwarder = agent._channel_tasks[CHANNEL_ID]

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": replacement.host,
            "port": replacement.port,
        })
        await wait_for_control(relay, "close")

        assert first.closed.is_set()
        assert first_forwarder.done()
        assert connect_calls == [(first.host, first.port)]
        assert agent._channels == {}
        assert agent._channel_tasks == {}
        assert [decode_control(message) for message in relay.sent] == [
            {"type": "connected", "channel": CHANNEL_ID},
            {"type": "close", "channel": CHANNEL_ID},
        ]
        relay.sent.clear()

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": replacement.host,
            "port": replacement.port,
        })
        await asyncio.wait_for(replacement.connected.wait(), timeout=1)
        await wait_for_control(relay, "connected")

        _, replacement_writer = agent._channels[CHANNEL_ID]
        assert replacement_writer.closed is False
        assert agent._channel_tasks[CHANNEL_ID].done() is False
        assert connect_calls == [
            (first.host, first.port),
            (replacement.host, replacement.port),
        ]
        assert [decode_control(message) for message in relay.sent] == [
            {"type": "connected", "channel": CHANNEL_ID},
        ]

        await agent.stop()

    asyncio.run(scenario())


def test_stop_cancels_in_flight_target_connect(monkeypatch):
    async def scenario():
        pending_service = PendingTcpService()
        relay = RelaySocket()
        monkeypatch.setattr(
            agent_module.asyncio,
            "open_connection",
            pending_service.connect,
        )
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{pending_service.host}:{pending_service.port}"},
        )
        agent._ws = relay

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": pending_service.host,
            "port": pending_service.port,
        })

        await asyncio.wait_for(pending_service.started.wait(), timeout=1)
        await asyncio.wait_for(agent.stop(), timeout=1)

        await agent._dispatch_control({
            "type": "connect",
            "channel": "22222222-2222-2222-2222-222222222222",
            "host": pending_service.host,
            "port": pending_service.port,
        })

        assert pending_service.cancelled.is_set()
        assert relay.closed is True
        assert agent._pending_connect_tasks == {}

    asyncio.run(scenario())


def test_relay_disconnect_during_connect_ack_closes_local_target(monkeypatch):
    async def scenario():
        class DisconnectedRelaySocket(RelaySocket):
            async def send(self, _message):
                raise ConnectionError("relay disconnected")

        service = IdleTcpService()
        relay = DisconnectedRelaySocket()
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{service.host}:{service.port}"},
        )
        agent._ws = relay

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": service.host,
            "port": service.port,
        })
        task = agent._pending_connect_tasks[CHANNEL_ID]
        await task
        await asyncio.sleep(0)

        assert service.closed.is_set()
        assert agent._pending_connect_tasks == {}
        assert agent._channels == {}

    asyncio.run(scenario())


def test_relay_close_during_connect_ack_closes_local_target(monkeypatch):
    async def scenario():
        class BlockingAckRelaySocket(RelaySocket):
            def __init__(self):
                super().__init__()
                self.ack_started = asyncio.Event()

            async def send(self, message):
                if decode_control(message).get("type") == "connected":
                    self.ack_started.set()
                    await asyncio.Event().wait()
                await super().send(message)

        service = IdleTcpService()
        relay = BlockingAckRelaySocket()
        monkeypatch.setattr(agent_module.asyncio, "open_connection", service.connect)
        agent = BridgeAgent(
            "wss://relay.example/tunnel",
            "cbr_test",
            {f"{service.host}:{service.port}"},
        )
        agent._ws = relay

        await agent._dispatch_control({
            "type": "connect",
            "channel": CHANNEL_ID,
            "host": service.host,
            "port": service.port,
        })
        await asyncio.wait_for(relay.ack_started.wait(), timeout=1)
        await asyncio.wait_for(
            agent._dispatch_control({"type": "close", "channel": CHANNEL_ID}),
            timeout=1,
        )

        assert service.closed.is_set()
        assert agent._pending_connect_tasks == {}
        assert agent._channels == {}
        assert agent._channel_tasks == {}
        assert [decode_control(message) for message in relay.sent] == [
            {"type": "close", "channel": CHANNEL_ID},
        ]

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


def test_connect_and_serve_without_allowlist_proxies_postgres_ssl_request_over_real_tls_and_tcp_sockets(
    monkeypatch,
):
    async def scenario():
        ssl_request = b"\x00\x00\x00\x08\x04\xd2\x16\x2f"
        ssl_response = b"N"
        tcp_closed = asyncio.Event()
        tcp_request = asyncio.get_running_loop().create_future()
        relay_done = asyncio.get_running_loop().create_future()

        async def postgres_server(reader, writer):
            try:
                request = await reader.readexactly(len(ssl_request))
                tcp_request.set_result(request)
                writer.write(ssl_response)
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                tcp_closed.set()

        tcp_server = await asyncio.start_server(postgres_server, "127.0.0.1", 0)
        tcp_port = tcp_server.sockets[0].getsockname()[1]

        async def relay_server(connection):
            try:
                path = connection.request.path
                await connection.send(json.dumps({
                    "type": "welcome",
                    "bridge_id": "bridge_integration",
                }))
                await connection.send(json.dumps({
                    "type": "connect",
                    "channel": CHANNEL_ID,
                    "host": "127.0.0.1",
                    "port": tcp_port,
                }))
                connected = decode_control(await connection.recv())

                await connection.send(CHANNEL_ID.encode("utf-8") + ssl_request)
                response_frame = await connection.recv()

                await connection.send(json.dumps({
                    "type": "close",
                    "channel": CHANNEL_ID,
                }))
                close = decode_control(await connection.recv())
                relay_done.set_result((path, connected, response_frame, close))
            except Exception as error:
                if not relay_done.done():
                    relay_done.set_exception(error)

        agent = None
        agent_task = None
        relay_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        relay_tls.load_cert_chain(TLS_CERTIFICATE, TLS_PRIVATE_KEY)
        monkeypatch.setenv("SSL_CERT_FILE", str(TLS_CERTIFICATE))
        try:
            async with serve(
                relay_server,
                "127.0.0.1",
                0,
                ssl=relay_tls,
            ) as websocket_server:
                relay_port = websocket_server.sockets[0].getsockname()[1]
                agent = BridgeAgent(
                    f"wss://127.0.0.1:{relay_port}/bridge?region=eu",
                    "cbr_integration_token",
                    set(),
                )
                agent_task = asyncio.create_task(agent._connect_and_serve())

                path, connected, response_frame, close = await asyncio.wait_for(
                    relay_done,
                    timeout=2,
                )
                await asyncio.wait_for(agent_task, timeout=2)

            request = await asyncio.wait_for(tcp_request, timeout=2)
            await asyncio.wait_for(tcp_closed.wait(), timeout=2)
            assert path == "/bridge?region=eu&token=cbr_integration_token"
            assert connected == {"type": "connected", "channel": CHANNEL_ID}
            assert request == ssl_request
            assert response_frame == CHANNEL_ID.encode("utf-8") + ssl_response
            assert close == {"type": "close", "channel": CHANNEL_ID}
            assert agent.allowed_hosts == set()
            assert agent._channels == {}
            assert agent._channel_tasks == {}
            assert agent._ws is None
        finally:
            if agent:
                await agent.stop()
            if agent_task:
                await asyncio.gather(agent_task, return_exceptions=True)
            tcp_server.close()
            await tcp_server.wait_closed()

    asyncio.run(scenario())


def test_connect_and_serve_configured_allowlist_denies_target_over_real_tls_without_opening_tcp_socket(
    monkeypatch,
):
    async def scenario():
        target_connected = asyncio.Event()
        relay_done = asyncio.get_running_loop().create_future()

        async def target_server(_reader, writer):
            target_connected.set()
            writer.close()
            await writer.wait_closed()

        tcp_server = await asyncio.start_server(target_server, "127.0.0.1", 0)
        tcp_port = tcp_server.sockets[0].getsockname()[1]

        async def relay_server(connection):
            try:
                path = connection.request.path
                await connection.send(json.dumps({
                    "type": "welcome",
                    "bridge_id": "bridge_allowlist",
                }))
                await connection.send(json.dumps({
                    "type": "connect",
                    "channel": CHANNEL_ID,
                    "host": "127.0.0.1",
                    "port": tcp_port,
                }))
                denial = decode_control(await connection.recv())
                relay_done.set_result((path, denial))
            except Exception as error:
                if not relay_done.done():
                    relay_done.set_exception(error)

        allowed_hosts = {"postgres-primary.internal:5432"}
        agent = None
        agent_task = None
        relay_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        relay_tls.load_cert_chain(TLS_CERTIFICATE, TLS_PRIVATE_KEY)
        monkeypatch.setenv("SSL_CERT_FILE", str(TLS_CERTIFICATE))
        try:
            async with serve(
                relay_server,
                "127.0.0.1",
                0,
                ssl=relay_tls,
            ) as websocket_server:
                relay_port = websocket_server.sockets[0].getsockname()[1]
                agent = BridgeAgent(
                    f"wss://127.0.0.1:{relay_port}/bridge",
                    "cbr_allowlist_token",
                    allowed_hosts,
                )
                agent_task = asyncio.create_task(agent._connect_and_serve())

                path, denial = await asyncio.wait_for(relay_done, timeout=2)
                await asyncio.wait_for(agent_task, timeout=2)

            assert path == "/bridge?token=cbr_allowlist_token"
            assert denial == {
                "type": "error",
                "channel": CHANNEL_ID,
                "message": (
                    f"Host 127.0.0.1:{tcp_port} is not in the allowed hosts list"
                ),
            }
            assert target_connected.is_set() is False
            assert agent.allowed_hosts == allowed_hosts
            assert agent._channels == {}
            assert agent._channel_tasks == {}
            assert agent._ws is None
        finally:
            if agent:
                await agent.stop()
            if agent_task:
                await asyncio.gather(agent_task, return_exceptions=True)
            tcp_server.close()
            await tcp_server.wait_closed()

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


def test_run_stops_immediately_on_auth_failure_401(monkeypatch, caplog):
    """When relay returns 401, run() logs auth failure and returns without retrying."""
    async def scenario():
        call_count = 0

        async def fake_connect_and_serve(self):
            nonlocal call_count
            call_count += 1
            response = Response(401, "Unauthorized", Headers())
            raise websockets.exceptions.InvalidStatus(response)

        sleep = MagicMock()
        monkeypatch.setattr(agent_module.asyncio, "sleep", sleep)
        monkeypatch.setattr(BridgeAgent, "_connect_and_serve", fake_connect_and_serve)
        agent = BridgeAgent("wss://relay.example", "cbr_bad_token", {"postgres:5432"})
        await agent.run()

        assert call_count == 1
        sleep.assert_not_called()
        assert "Authentication failed: the bridge token was rejected by the relay." in caplog.text

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
