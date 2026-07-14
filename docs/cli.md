# CLI Manual

py-frp provides TCP reverse forwarding through a public server. It accepts a
practical subset of frp TOML/INI and rathole TOML configuration, but it does not
implement either project's wire protocol.

## Installation and entry points

Install the published package:

```bash
pip install py-simple-nat-tunnel
```

For development from a checkout, use `pip install -e .`. The installation
provides these equivalent entry points:

| Command | Purpose |
| --- | --- |
| `py-frp server ...` | Run the public server |
| `py-frp frps ...` | Alias for `py-frp server` |
| `py-frps ...` | Standalone server entry point |
| `py-frp client ...` | Run the private client |
| `py-frp frpc ...` | Alias for `py-frp client` |
| `py-frpc ...` | Standalone client entry point |
| `pyfrp ...` | Alias for `py-frp ...` |
| `python -m py_frp ...` | Module entry point, useful when scripts are not on `PATH` |

Run `py-frp --version` to print the installed version. Run `py-frp server
--help` or `py-frp client --help` for command-specific help.

## Server command

```text
py-frp server [-c PATH | --port-pool PORTS] [options]
```

The server requires either a configuration file or at least one `--port-pool`.

| Option | Default | Description |
| --- | --- | --- |
| `-c`, `--config PATH` | none | Load server settings from TOML, JSON, INI, or CONF |
| `--bind-host HOST` | `0.0.0.0` | Control listener host in configless mode |
| `--bind-port PORT` | `7000` | Control listener port in configless mode |
| `--port-pool SPEC` | none | Public port or inclusive range; repeatable and comma-aware |
| `--pool-bind-host HOST` | control bind host | Public listener host in pool mode |
| `--token-length N` | `24` | Length of the generated shared pool token |
| `--elevate` | off | Request one administrator/root relaunch before binding |
| `--auto-elevate`, `--no-auto-elevate` | on | Enable or disable automatic elevation for ports below 1024 |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

### Configless port-pool mode

Start a control listener and make ports 6000 through 6009 available:

```bash
py-frp server --bind-port 7000 --port-pool 6000-6009
```

`--port-pool` may be repeated, and each value may contain comma-separated
ports or ranges:

```bash
py-frp server --port-pool 6000-6009,7000 --port-pool 7100-7102
```

Duplicate ports are removed. Ranges are inclusive. On startup, the server
prints its TLS fingerprint and one randomly generated shared token:

```text
tls_fingerprint SHA256:12:34:...:AB
py-frp token pool
control 0.0.0.0:7000
public_bind 0.0.0.0
token GENERATED_TOKEN
```

All authorized pool clients use this token. Each connected client receives the
lowest currently available port. A port already occupied by another process is
skipped. When no port is available, the new client is rejected without
disconnecting existing clients.

### Configuration-file mode

The included frp-style example is enough for dynamic TCP services:

```toml
bindPort = 7000

[auth]
token = "change-me"
```

The server accepts `.ini` and `.conf` as legacy frp INI. A `.json` suffix is
parsed as JSON; every other suffix is parsed as TOML. Supported server settings
include the control bind address/port, default token, open timeout, dynamic
service policy, and preconfigured TCP services.

A rathole-style server configuration predefines its services and disables
dynamic services by default:

```toml
[server]
bind_addr = "0.0.0.0:2333"
default_token = "change-me"

[server.services.ssh]
bind_addr = "0.0.0.0:6000"
```

## Client command

```text
py-frp client [-c PATH | --server HOST:PORT --token TOKEN] [options]
```

The client requires either a configuration file or both `--server` and
`--token`.

| Option | Default | Description |
| --- | --- | --- |
| `-c`, `--config PATH` | none | Load client settings from TOML, JSON, INI, or CONF |
| `--server HOST:PORT` | none | Control address in configless mode; IPv6 may use `[addr]:port` |
| `--token TOKEN` | none | Shared token in configless mode |
| `--server-fingerprint VALUE` | interactive | Pin the server certificate's SHA-256 fingerprint |
| `--local HOST:PORT` | `127.0.0.1:22` | TCP target reachable from the client |
| `--reconnect-delay SECONDS` | `3.0` | Delay after a recoverable control disconnect |
| `--connect-timeout SECONDS` | `10.0` | Timeout while opening the local target |
| `--heartbeat-interval SECONDS` | `30.0` | Interval between control-channel pings |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

All three time values must be greater than zero.

### Configless client

Connect to a pool server and forward the local SSH service:

```bash
py-frp client --server your-server:7000 --token GENERATED_TOKEN
```

To forward another local service or another device reachable on the client's
LAN, set `--local`:

```bash
py-frp client --server your-server:7000 --token GENERATED_TOKEN \
  --local 192.168.1.50:8080
```

After registration, the assigned public port is printed as a line containing
only the port number.

### Configuration-file client

frp-style TOML:

```toml
serverAddr = "your-server"
serverPort = 7000
serverFingerprint = "SHA256:12:34:...:AB"

[auth]
token = "change-me"

[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 6000
```

Legacy frp INI:

```ini
[common]
server_addr = your-server
server_port = 7000
token = change-me
server_fingerprint = SHA256:12:34:...:AB

[ssh]
type = tcp
local_ip = 127.0.0.1
local_port = 22
remote_port = 6000
```

rathole-style TOML:

```toml
[client]
remote_addr = "your-server:2333"
default_token = "change-me"
server_fingerprint = "SHA256:12:34:...:AB"

[client.services.ssh]
local_addr = "127.0.0.1:22"
```

Only TCP proxies are accepted. Multiple proxy/service entries may be defined in
one client file. The local host can be the client itself or any TCP endpoint
reachable from it.

## TLS fingerprint verification

Every control and tunnel connection between the server and client uses TLS 1.2
or newer. The server creates a new ephemeral certificate at each startup and
prints its SHA-256 fingerprint.

Without a configured fingerprint, the client prints the received fingerprint
and asks:

```text
Trust this server fingerprint? [y/N]:
```

Compare it with the server output through a trusted channel before answering
`y`. The accepted fingerprint is pinned for all later connections in that
client process. Since the certificate is ephemeral, a server restart requires
confirmation again.

For unattended use, pass `--server-fingerprint` or set `serverFingerprint`
(frp TOML), `server_fingerprint` in `[common]` (frp INI), or
`server_fingerprint` in `[client]` (rathole TOML). Fingerprints may be written
with or without `SHA256:` and colon separators, but must contain exactly 32
SHA-256 bytes.

TLS protects only the server-to-client segment. The public connection to the
server and the client connection to the target are separate plain TCP segments;
use an application protocol such as SSH or HTTPS when end-to-end encryption is
required.

## Privileged ports

When any configured control, service, or pool listener uses a port below 1024,
the server tries to relaunch once through `py-admin-launch`. Use `--elevate` to
request elevation explicitly, or `--no-auto-elevate` to disable the automatic
behavior.

## Exit status and logging

| Status | Meaning |
| --- | --- |
| `0` | Normal completion; the client also uses this after logging a fatal TLS, authentication, or pool-exhaustion response |
| `1` | Unhandled runtime or elevation failure |
| `2` | CLI parsing or configuration error |
| `130` | Interrupted with Ctrl+C |

Logs go to stderr. Generated pool credentials, TLS fingerprints, and assigned
pool ports go to stdout so they can be captured separately.

## Current limits

py-frp supports TCP reverse forwarding, multiple services, token
authentication, configless port pools, TLS fingerprint pinning, and the config
subsets shown above. It does not currently support UDP, HTTP/HTTPS virtual
hosts, frp plugins, load balancing, STCP/XTCP, rathole wire compatibility, or
connection multiplexing. Each public TCP connection creates one independent
tunnel TCP connection.
