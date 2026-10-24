"""
connic bridge - runs inside the customer's VPC.

Establishes an outbound WebSocket connection to the connic relay service
and proxies TCP connections to private services (Kafka, PostgreSQL, etc.)
on behalf of connic connector workers.
"""
import asyncio
import json
import logging
import signal
from typing import Dict, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit

import websockets
from websockets.asyncio.client import ClientConnection, connect

logger = logging.getLogger("connic-bridge")
transport_logger = logging.getLogger("connic-bridge.websocket")
transport_logger.setLevel(logging.WARNING)

TCP_WRITE_QUEUE_MAXSIZE = 16
TCP_WRITE_TIMEOUT = 10.0
TCP_CLOSE_TIMEOUT = 5.0
# Per-channel flow control: each side announces how many data frames it
# buffers per channel and returns credit as it writes them onward.
WINDOW_HEADER = "X-Connic-Bridge-Window"


def parse_window(value: Optional[str]) -> Optional[int]:
    try:
        window = int(value)
    except (TypeError, ValueError):
        return None
    return window if window > 0 else None


class RelayConnect(connect):
    def process_redirect(self, exc):
        target = super().process_redirect(exc)
        if isinstance(target, str):
            current_url, target_url = urlsplit(self.uri), urlsplit(target)
            if (current_url.hostname, current_url.port or 443) != (
                target_url.hostname, target_url.port or 443
            ):
                return exc
        return target


def validate_relay_url(relay_url: str) -> None:
    if urlsplit(relay_url).scheme != "wss":
        raise ValueError("Relay URL must use wss:// to protect bridge traffic.")


class BridgeAgent:
    """
    Bridge agent that connects to the relay and proxies tunnel requests.

    The agent:
    1. Connects to the relay via WSS with a bridge token
    2. Receives CONNECT requests from the relay
    3. Opens local TCP connections to the requested targets
    4. Proxies data bidirectionally over the WebSocket
    """

    def __init__(
        self,
        relay_url: str,
        token: str,
        allowed_hosts: Set[str],
    ):
        validate_relay_url(relay_url)
        self.relay_url = relay_url
        self.token = token
        self.allowed_hosts = allowed_hosts
        self._ws: Optional[ClientConnection] = None
        self._channels: Dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self._channel_tasks: Dict[str, asyncio.Task] = {}
        self._channel_write_queues: Dict[str, asyncio.Queue] = {}
        self._channel_write_tasks: Dict[str, asyncio.Task] = {}
        self._channel_workers: Set[asyncio.Task] = set()
        self._pending_connect_tasks: Dict[str, asyncio.Task] = {}
        # Frames the relay buffers per channel; None when it has no flow control.
        self._relay_window: Optional[int] = None
        self._send_credits: Dict[str, int] = {}
        self._credit_available: Dict[str, asyncio.Event] = {}
        self._reconnect_task: Optional[asyncio.Task] = None
        self._relay_connected = False
        self._running = False
        self._stopping = False

    async def run(self):
        """Main loop with automatic reconnection."""
        self._running = True
        self._stopping = False
        base_delay = 2
        max_delay = 60
        delay = base_delay

        while self._running:
            self._relay_connected = False
            try:
                await self._connect_and_serve()
                delay = base_delay  # Reset on clean disconnect
            except (
                websockets.exceptions.InvalidStatusCode,
                websockets.exceptions.InvalidStatus,
            ) as e:
                # InvalidStatusCode (websockets <13) / InvalidStatus (>=13)
                status = getattr(e, "status_code", None) or getattr(
                    getattr(e, "response", None), "status_code", None
                )
                if status in (403, 401, 4003):
                    logger.error(
                        "Authentication failed: the bridge token was rejected by the relay. "
                        "Please check that BRIDGE_TOKEN is set correctly."
                    )
                    return
                logger.error(f"Connection rejected (HTTP {status}), retrying in {delay}s...")
            except websockets.exceptions.ConnectionClosed as e:
                if e.rcvd is not None and e.rcvd.code == 4003:
                    logger.error(
                        "Authentication failed: the bridge token was rejected by the relay. "
                        "Please check that BRIDGE_TOKEN is set correctly."
                    )
                    return
                logger.warning(f"Connection lost: {e}")
            except (ConnectionError, OSError) as e:
                logger.warning(f"Connection lost: {e}")
            except Exception as e:
                logger.error(f"Unexpected error: {e}")

            if self._relay_connected:
                delay = base_delay  # Reset once a connection was established
            if self._running:
                logger.info(f"Reconnecting in {delay}s...")
                self._reconnect_task = asyncio.create_task(asyncio.sleep(delay))
                try:
                    await self._reconnect_task
                except asyncio.CancelledError:
                    if self._running:
                        raise
                finally:
                    self._reconnect_task = None
                delay = min(delay * 2, max_delay)

    async def stop(self):
        """Gracefully shut down the agent."""
        self._running = False
        self._stopping = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        await self._cancel_pending_connects()
        tasks = list(self._channel_workers)
        # Close all channels
        for channel_id in list(self._channels.keys()):
            await self._close_channel(channel_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Close WebSocket
        if self._ws:
            await self._ws.close()

    async def _connect_and_serve(self):
        """Connect to relay and process messages."""
        relay_url = urlsplit(self.relay_url)
        query = urlencode([
            (key, value) for key, value in parse_qsl(relay_url.query, keep_blank_values=True)
            if key != "token"
        ])
        url = relay_url._replace(query=query).geturl()
        logger.info("Connecting to relay: %s", relay_url.hostname)

        async with RelayConnect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.token}",
                WINDOW_HEADER: str(TCP_WRITE_QUEUE_MAXSIZE),
            },
            ping_interval=30,
            ping_timeout=10,
            logger=transport_logger,
        ) as ws:
            self._ws = ws
            self._relay_connected = True
            self._relay_window = parse_window(ws.response.headers.get(WINDOW_HEADER))
            try:
                if self._stopping:
                    return
                logger.info("Connected to relay")
                async for message in ws:
                    if isinstance(message, str):
                        # Control message (JSON)
                        try:
                            ctrl = json.loads(message)
                        except json.JSONDecodeError:
                            logger.warning("Received invalid JSON")
                            continue
                        await self._dispatch_control(ctrl)

                    elif isinstance(message, bytes):
                        # Data frame: first 36 bytes = channel UUID
                        if len(message) < 36:
                            continue
                        channel_id = message[:36].decode("utf-8")
                        payload = message[36:]
                        await self._handle_data(channel_id, payload)
            finally:
                self._ws = None
                await self._cancel_pending_connects()
                tasks = list(self._channel_workers)
                for channel_id in set(self._channels) | set(self._channel_tasks):
                    await self._close_channel(channel_id)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch_control(self, ctrl: dict):
        if self._stopping:
            return
        if ctrl.get("type") != "connect":
            await self._handle_control(ctrl)
            return

        channel_id = ctrl["channel"]
        await self._cancel_pending_connects(channel_id)
        if self._stopping:
            return
        task = asyncio.create_task(
            self._open_channel_in_background(channel_id, ctrl["host"], ctrl["port"])
        )
        self._pending_connect_tasks[channel_id] = task
        task.add_done_callback(
            lambda done, channel_id=channel_id: self._forget_pending_connect(
                channel_id, done
            )
        )

    async def _open_channel_in_background(
        self, channel_id: str, host: str, port: int
    ):
        try:
            await self._open_channel(channel_id, host, port)
        except asyncio.CancelledError:
            await self._close_channel(channel_id)
            raise
        except Exception as e:
            logger.error(f"Unexpected error opening channel {channel_id[:8]}...: {e}")
            await self._close_channel(channel_id)

    def _forget_pending_connect(self, channel_id: str, task: asyncio.Task):
        if self._pending_connect_tasks.get(channel_id) is task:
            self._pending_connect_tasks.pop(channel_id)

    async def _cancel_pending_connects(self, channel_id: Optional[str] = None):
        if channel_id is None:
            tasks = list(self._pending_connect_tasks.values())
        else:
            task = self._pending_connect_tasks.get(channel_id)
            tasks = [task] if task else []

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_control(self, ctrl: dict):
        msg_type = ctrl.get("type")

        if msg_type == "welcome":
            bridge_id = ctrl.get("bridge_id", "unknown")
            logger.info(f"Bridge authenticated (bridge_id={bridge_id})")
            logger.info(
                f"Allowed hosts: {', '.join(sorted(self.allowed_hosts)) or '(unrestricted)'}"
            )

        elif msg_type == "connect":
            channel_id = ctrl["channel"]
            host = ctrl["host"]
            port = ctrl["port"]
            await self._open_channel(channel_id, host, port)

        elif msg_type == "close":
            channel_id = ctrl.get("channel")
            if channel_id:
                await self._cancel_pending_connects(channel_id)
                await self._close_channel(channel_id)

        elif msg_type == "window":
            channel_id = ctrl.get("channel")
            frames = ctrl.get("frames")
            if (
                channel_id in self._send_credits
                and isinstance(frames, int)
                and not isinstance(frames, bool)
                and frames > 0
            ):
                self._send_credits[channel_id] = min(
                    self._send_credits[channel_id] + frames,
                    self._relay_window,
                )
                self._credit_available[channel_id].set()

    async def _handle_data(self, channel_id: str, data: bytes):
        """Forward data from relay to the local TCP connection."""
        queue = self._channel_write_queues.get(channel_id)
        if queue is None:
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            await self._close_channel(channel_id, flush_pending=False)
        else:
            await asyncio.sleep(0)

    async def _forward_relay_to_tcp(self, channel_id, writer, queue):
        try:
            while True:
                writer.write(await queue.get())
                frames = 1
                while not queue.empty():
                    writer.write(queue.get_nowait())
                    frames += 1
                await asyncio.wait_for(writer.drain(), timeout=TCP_WRITE_TIMEOUT)
                if self._channel_write_queues.get(channel_id) is not queue:
                    break
                await self._send_control({
                    "type": "window",
                    "channel": channel_id,
                    "frames": frames,
                })
        except (
            ConnectionError,
            OSError,
            asyncio.TimeoutError,
            asyncio.CancelledError,
            websockets.exceptions.ConnectionClosed,
        ):
            pass
        finally:
            await self._close_channel(channel_id, flush_pending=False)

    async def _open_channel(self, channel_id: str, host: str, port: int):
        """Open a TCP connection to the target and start proxying."""
        target = f"{host}:{port}"

        if channel_id in self._channels or channel_id in self._channel_tasks:
            logger.warning(f"Rejecting duplicate active channel {channel_id[:8]}...")
            await self._close_channel(channel_id)
            return

        # Validate against allowed hosts when an allowlist is configured
        if self.allowed_hosts and target not in self.allowed_hosts:
            logger.warning(f"Blocked connection to {target} (not in allowed hosts)")
            await self._send_control({
                "type": "error",
                "channel": channel_id,
                "message": f"Host {target} is not in the allowed hosts list",
            })
            return

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"Connection to {target} timed out")
            await self._send_control({
                "type": "error",
                "channel": channel_id,
                "message": f"Connection to {target} timed out",
            })
            return
        except OSError as e:
            logger.error(f"Failed to connect to {target}: {e}")
            await self._send_control({
                "type": "error",
                "channel": channel_id,
                "message": f"Failed to connect to {target}: {e}",
            })
            return

        self._channels[channel_id] = (reader, writer)
        queue = asyncio.Queue(maxsize=TCP_WRITE_QUEUE_MAXSIZE)
        self._channel_write_queues[channel_id] = queue
        if self._relay_window is not None:
            self._send_credits[channel_id] = self._relay_window
            self._credit_available[channel_id] = asyncio.Event()

        # Signal success
        await self._send_control({"type": "connected", "channel": channel_id})
        if self._channels.get(channel_id) != (reader, writer):
            return
        logger.info(f"Channel {channel_id[:8]}... opened to {target}")

        write_task = asyncio.create_task(self._forward_relay_to_tcp(channel_id, writer, queue))
        self._channel_write_tasks[channel_id] = write_task
        self._channel_workers.add(write_task)
        write_task.add_done_callback(self._channel_workers.discard)

        # Start forwarding local TCP -> relay
        task = asyncio.create_task(self._forward_tcp_to_relay(channel_id, reader))
        self._channel_tasks[channel_id] = task
        self._channel_workers.add(task)
        task.add_done_callback(self._channel_workers.discard)

    async def _forward_tcp_to_relay(self, channel_id: str, reader: asyncio.StreamReader):
        """Read from local TCP and send to relay as binary frames."""
        try:
            while True:
                # Wait for relay credit before reading so the target sees backpressure.
                await self._take_send_credit(channel_id)
                data = await reader.read(65536)
                if not data:
                    break
                frame = channel_id.encode("utf-8") + data
                if self._ws:
                    await self._ws.send(frame)
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass
        finally:
            await self._close_channel(channel_id)

    async def _take_send_credit(self, channel_id: str):
        available = self._credit_available.get(channel_id)
        if available is None:
            return
        while self._send_credits.get(channel_id, 0) <= 0:
            available.clear()
            await available.wait()
        self._send_credits[channel_id] -= 1

    async def _close_channel(self, channel_id: str, flush_pending: bool = True):
        """Close a tunnel channel and clean up."""
        pair = self._channels.pop(channel_id, None)
        task = self._channel_tasks.pop(channel_id, None)
        write_task = self._channel_write_tasks.pop(channel_id, None)
        queue = self._channel_write_queues.pop(channel_id, None)
        self._send_credits.pop(channel_id, None)
        self._credit_available.pop(channel_id, None)
        if pair is None and task is None and write_task is None:
            return

        tasks = [
            worker for worker in (task, write_task)
            if worker and worker is not asyncio.current_task() and not worker.done()
        ]
        for worker in tasks:
            worker.cancel()

        if pair:
            _, writer = pair
            try:
                try:
                    if flush_pending and queue is not None:
                        while not queue.empty():
                            writer.write(queue.get_nowait())
                finally:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=TCP_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                writer.transport.abort()
            except asyncio.CancelledError:
                writer.transport.abort()
                raise
            except Exception:
                pass

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Notify relay
        try:
            await self._send_control({"type": "close", "channel": channel_id})
        except Exception:
            pass

    async def _send_control(self, message: dict):
        if self._ws:
            await self._ws.send(json.dumps(message))


async def run_agent(relay_url: str, token: str, allowed_hosts: Set[str]):
    """Run the bridge with signal handling."""
    agent = BridgeAgent(relay_url, token, allowed_hosts)

    loop = asyncio.get_event_loop()

    def shutdown():
        logger.info("Shutdown signal received")
        asyncio.ensure_future(agent.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass  # Windows

    await agent.run()
