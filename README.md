# Connic Bridge

Connect your private infrastructure to Connic Cloud. The Connic Bridge runs
inside your network (VPC, on-prem, etc.) and creates a secure outbound tunnel
to the Connic relay. No inbound firewall rules required.

## Quick Start

### Docker (recommended)

```bash
docker run -d --name connic-bridge \
  -e BRIDGE_TOKEN=cbr_your_token_here \
  -e ALLOWED_HOSTS=kafka:9092,postgres:5432 \
  connicorg/bridge:latest
```

### pip

```bash
pip install connic-bridge

connic-bridge \
  --token cbr_your_token_here \
  --allow kafka:9092 \
  --allow postgres:5432
```

## Configuration


| Option        | Env Var         | Description                                                 |
| ------------- | --------------- | ----------------------------------------------------------- |
| `--token`     | `BRIDGE_TOKEN`  | Bridge authentication token (from Connic dashboard)         |
| `--relay-url` | `RELAY_URL`     | Relay URL (default: `wss://relay.connic.co`)                |
| `--allow`     | `ALLOWED_HOSTS` | Comma-separated `host:port` pairs the bridge may connect to |
| `--log-level` | `LOG_LEVEL`     | `DEBUG`, `INFO`, `WARNING`, or `ERROR`                      |


## How It Works

1. The Connic Bridge establishes an **outbound** WebSocket connection to the
  Connic relay service. No inbound ports need to be opened.
2. When a Connic connector (e.g. Kafka, PostgreSQL) needs to reach a private
  endpoint, the relay asks the bridge to open a TCP connection.
3. The bridge validates the target against the allowed hosts list, opens a
  local TCP connection, and proxies data bidirectionally.
4. All traffic is encrypted via WSS (TLS).

## Connect Services That Discover Endpoints

Some clients receive a different hostname or IP address after their first
connection. Automatic destination routes send those follow-up connections
through the correct bridge without requiring changes to your client code.

Configure routes under **Project Settings → Bridge**, and add every permitted
`host:port` to `ALLOWED_HOSTS`.

See [the automatic destination routes guide](https://connic.co/docs/v1/platform/bridge#use-tools-middlewares)
for setup instructions, API usage, supported patterns, and limits.

## Security

- **Outbound-only** - the bridge never accepts inbound connections.
- **Allowed hosts** - you control exactly which services the bridge can reach.
- **Token authentication** - each bridge is tied to a single Connic project.
- **TLS encryption** - all relay communication uses WSS.
