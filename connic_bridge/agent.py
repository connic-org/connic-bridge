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
import sys
from typing import Dict, Optional, Set

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
        self._running = False

    async def run(self):
        """Main loop with automatic reconnection."""
        self._running = True
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
            except (ConnectionError, OSError, websockets.exceptions.ConnectionClosed) as e:
                logger.warning(f"Connection lost: {e}")
            except Exception as e:
                logger.error(f"Unexpected error: {e}")

            if self._running:
                logger.info(f"Reconnecting in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    async def stop(self):
        """Gracefully shut down the agent."""
        self._running = False
        # Close all channels
        for channel_id in list(self._channels.keys()):
            await self._close_channel(channel_id)
        # Close WebSocket
        if self._ws:
            await self._ws.close()

    async def _connect_and_serve(self):
        """Connect to relay and process messages."""
        url = f"{self.relay_url}?token={self.token}"
        logger.info(f"Connecting to relay: {self.relay_url}")

        async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
            self._ws = ws
            logger.info("Connected to relay")

            async for message in ws:
                if isinstance(message, str):
                    # Control message (JSON)
                    try:
                        ctrl = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("Received invalid JSON")
                        continue
                    await self._handle_control(ctrl)

                elif isinstance(message, bytes):
                    # Data frame: first 36 bytes = channel UUID
                    if len(message) < 36:
                        continue
                    channel_id = message[:36].decode("utf-8")
                    payload = message[36:]
                    await self._handle_data(channel_id, payload)

    async def _handle_control(self, ctrl: dict):
        msg_type = ctrl.get("type")

        if msg_type == "welcome":
            project_id = ctrl.get("project_id", "unknown")
            logger.info(f"Bridge authenticated for project {project_id}")
            logger.info(f"Allowed hosts: {', '.join(sorted(self.allowed_hosts))}")

        elif msg_type == "connect":
            channel_id = ctrl["channel"]
            host = ctrl["host"]
            port = ctrl["port"]
            await self._open_channel(channel_id, host, port)

        elif msg_type == "close":
            channel_id = ctrl.get("channel")
            if channel_id:
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
        if pair:
            _, writer = pair
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        task = self._channel_tasks.pop(channel_id, None)
        if task and not task.done():
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
