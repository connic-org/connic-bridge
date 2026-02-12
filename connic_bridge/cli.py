"""
CLI entrypoint for Connic Bridge.

Usage:
    connic-bridge --token cbr_xxx --allow kafka:9092,postgres:5432
    connic-bridge --token cbr_xxx --allow kafka:9092 --allow postgres:5432

Environment variables:
    BRIDGE_TOKEN     - Bridge authentication token
    RELAY_URL        - Relay WebSocket URL (default: wss://relay.connic.co)
    ALLOWED_HOSTS    - Comma-separated list of host:port pairs
"""
import argparse
import asyncio
import logging
import os
import sys

from connic_bridge.agent import run_agent

DEFAULT_RELAY_URL = "wss://relay.connic.co"


def main():
    parser = argparse.ArgumentParser(
        description="Connic Bridge - connect private networks to Connic Cloud",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BRIDGE_TOKEN"),
        help="Bridge authentication token (or set BRIDGE_TOKEN env var)",
    )
    parser.add_argument(
        "--relay-url",
        default=os.environ.get("RELAY_URL", DEFAULT_RELAY_URL),
        help=f"Relay WebSocket URL (default: {DEFAULT_RELAY_URL})",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Allowed host:port (can be specified multiple times). Also accepts comma-separated values.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    # Resolve token
    token = args.token
    if not token:
        print("Error: Bridge token is required. Use --token or set BRIDGE_TOKEN env var.")
        sys.exit(1)

    # Resolve allowed hosts (from --allow args and ALLOWED_HOSTS env)
    allowed_hosts = set()
    env_hosts = os.environ.get("ALLOWED_HOSTS", "")
    if env_hosts:
        for h in env_hosts.split(","):
            h = h.strip()
            if h:
                allowed_hosts.add(h)
    for entry in args.allow:
        for h in entry.split(","):
            h = h.strip()
            if h:
                allowed_hosts.add(h)

    if not allowed_hosts:
        print("Warning: No allowed hosts specified. The bridge will reject all connections.")
        print("Use --allow host:port or set ALLOWED_HOSTS env var.")

    # Run
    print(f"Connic Bridge starting...")
    print(f"  Relay:         {args.relay_url}")
    print(f"  Allowed hosts: {', '.join(sorted(allowed_hosts)) or '(none)'}")

    asyncio.run(run_agent(args.relay_url, token, allowed_hosts))


if __name__ == "__main__":
    main()
