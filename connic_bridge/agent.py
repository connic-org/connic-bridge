"""
Connic Bridge - runs inside the customer's VPC.

Establishes an outbound WebSocket connection to the Connic relay service
and proxies TCP connections to private services (Kafka, PostgreSQL, etc.)
on behalf of Connic connector workers.
"""
import asyncio
import json
import logging
import signal
from typing import Dict, Optional, Set
from urllib.parse import urlencode, urlsplit

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger("connic-bridge")


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
        self.relay_url = relay_url
        self.token = token
        self.allowed_hosts = allowed_hosts
        self._ws: Optional[ClientConnection] = None
        self._channels: Dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self._channel_tasks: Dict[str, asyncio.Task] = {}
        self._pending_connect_tasks: Dict[str, asyncio.Task] = {}
        self._reconnect_task: Optional[asyncio.Task] = None
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
        # Close all channels
        for channel_id in list(self._channels.keys()):
            await self._close_channel(channel_id)
        # Close WebSocket
        if self._ws:
            await self._ws.close()

    async def _connect_and_serve(self):
        """Connect to relay and process messages."""
        relay_url = urlsplit(self.relay_url)
        token_query = urlencode({"token": self.token})
        query = f"{relay_url.query}&{token_query}" if relay_url.query else token_query
        url = relay_url._replace(query=query).geturl()
        logger.info(f"Connecting to relay: {self.relay_url}")

        async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
            self._ws = ws
            logger.info("Connected to relay")

            try:
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
                tasks = list(self._channel_tasks.values())
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
            logger.info(f"Allowed hosts: {', '.join(sorted(self.allowed_hosts))}")

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

    async def _handle_data(self, channel_id: str, data: bytes):
        """Forward data from relay to the local TCP connection."""
        pair = self._channels.get(channel_id)
        if not pair:
            return
        _, writer = pair
        try:
            writer.write(data)
            await writer.drain()
        except (ConnectionError, OSError):
            await self._close_channel(channel_id)

    async def _open_channel(self, channel_id: str, host: str, port: int):
        """Open a TCP connection to the target and start proxying."""
        target = f"{host}:{port}"

        # Validate against allowed hosts
        if target not in self.allowed_hosts:
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

        # Signal success
        await self._send_control({"type": "connected", "channel": channel_id})
        logger.info(f"Channel {channel_id[:8]}... opened to {target}")

        # Start forwarding local TCP -> relay
        task = asyncio.create_task(self._forward_tcp_to_relay(channel_id, reader))
        self._channel_tasks[channel_id] = task

    async def _forward_tcp_to_relay(self, channel_id: str, reader: asyncio.StreamReader):
        """Read from local TCP and send to relay as binary frames."""
        try:
            while True:
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

    async def _close_channel(self, channel_id: str):
        """Close a tunnel channel and clean up."""
        pair = self._channels.pop(channel_id, None)
        task = self._channel_tasks.pop(channel_id, None)
        if pair is None and task is None:
            return

        if pair:
            _, writer = pair
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

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
