# Client CLI

Use the client command on the private machine that can reach the target
service.

```text
py-frp client [-c PATH | --server HOST:PORT --token TOKEN] [options]
```

The client requires either a configuration file or both `--server` and
`--token`.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `-c`, `--config PATH` | none | Load client settings from TOML, JSON, INI, or CONF |
| `--server HOST:PORT` | none | Control address in configless mode; IPv6 may use `[addr]:port` |
| `--token TOKEN` | none | Shared pool token in configless mode |
| `--force` | off | Automatically allow pool preemption; configless mode only |
| `--priority N` | `0` | Integer pool priority; smaller numbers have higher priority; configless mode only |
| `--server-fingerprint VALUE` | interactive | Pin a full fingerprint or a fingerprint pattern containing `...` |
| `--local HOST:PORT` | `127.0.0.1:22` | TCP target reachable from the client |
| `--reconnect-delay SECONDS` | `3.0` | Delay after a recoverable control disconnect |
| `--connect-timeout SECONDS` | `10.0` | Timeout while opening the local target |
| `--heartbeat-interval SECONDS` | `30.0` | Interval between control-channel pings |
| `--auto-restart`, `--no-auto-restart` | on | Enable or disable restart after an installed package version change |
| `--update-check-interval SECONDS` | `5.0` | Seconds between installed package version checks |
| `--log-level LEVEL` | `INFO` | Logging threshold |

All numeric timeout and interval values must be greater than zero.

## Configless client

Forward the client's local SSH service through a pool server:

```bash
py-frp client --server your-server:7000 --token GENERATED_TOKEN
```

To forward another local service, including one on the client's LAN, set
`--local`:

```bash
py-frp client --server your-server:7000 --token GENERATED_TOKEN \
  --local 192.168.1.50:8080
```

After registration, the assigned public port is printed as a line containing
only the port number. See the [configless deployment tutorial](tutorial.md) for
a complete production flow.

## Forced connection and priority

If every pool port belongs to an online client, the server offers a forced
connection. Without `--force`, the client asks:

```text
Force connection and disconnect an eligible equal-or-lower-priority pool client? [y/N]:
```

Answering `y` retries registration with force enabled. Any other answer stops
the current client without evicting anyone. For unattended clients, make the
choice explicit:

```bash
py-frp client --server your-server:7000 --token GENERATED_TOKEN \
  --force --priority 3
```

Priority is any integer and defaults to `0`. Smaller numbers have higher actual
priority. An incoming forced client at priority `N` may evict only a client
whose number is greater than or equal to `N`; numbers smaller than `N` are
protected.

| Incoming forced client | Existing priorities | Result |
| --- | --- | --- |
| `3` | `-1, 2, 3, 7` | Evicts `7` |
| `3` | `1, 2` | Rejected because neither client is eligible |
| `3` | `3, 3` | Evicts the connection created first |

Among eligible clients, the server evicts the largest numeric priority first.
If several have the same number, it evicts the oldest control connection. A
rejected response reports `max_priority`, the largest number among current
connections.

No client is evicted merely because the pool is full. Preemption occurs only
after `--force` or an accepted prompt. The same rule applies during reconnect,
including reconnect after a server restart. A client launched with `--force`
keeps making forced requests with its original priority.

## Server fingerprint verification

Every control and tunnel connection uses TLS 1.2 or newer. The server prints an
actual 32-byte SHA-256 certificate fingerprint, for example:

```text
SHA256:12:34:56:78:90:AB:CD:EF:10:32:54:76:98:BA:DC:FE:01:23:45:67:89:AB:CD:EF:10:20:30:40:50:60:70:80
```

The server never prints `...`; that token is a client-side wildcard syntax.
Without `--server-fingerprint`, the client prints the received value and asks:

```text
Trust this server fingerprint? [y/N]:
```

Compare the complete value through a trusted channel before answering `y`.
The accepted value is pinned for every later control and tunnel connection.

### Full pin

The safest unattended form copies all 32 bytes exactly:

```bash
py-frp client --server your-server:7000 --token GENERATED_TOKEN \
  --server-fingerprint 'SHA256:12:34:56:78:90:AB:CD:EF:10:32:54:76:98:BA:DC:FE:01:23:45:67:89:AB:CD:EF:10:20:30:40:50:60:70:80'
```

`SHA256:` and colon separators are optional for a complete pin, but the value
must still contain exactly 32 bytes.

### `...` wildcard pin

One literal `...` matches zero or more complete bytes in the middle of the
32-byte fingerprint. Fixed bytes on each side remain anchored to the beginning
and end. For example:

```text
SHA256:12:34:...:70:80
```

matches the full example above because it begins with `12:34` and ends with
`70:80`. It does not match a fingerprint with a different prefix or suffix.
The wildcard may appear at the beginning or end. A pattern may contain only one
`...` and may not specify more than 32 fixed bytes.

A wildcard deliberately verifies fewer bits than a full pin. Every fixed byte
contributes eight bits; a pattern with only one fixed byte has just 1-in-256
selectivity and is unsafe against an active attacker. Use the complete
fingerprint whenever it can be transported reliably. The wildcard exists for
explicit partial-pin workflows, not as a replacement for certificate trust.

A bare `...` is valid and matches every 32-byte fingerprint. When a client
starts with that value, it logs a warning that every TLS certificate will match
and server identity is not verified. This retains encrypted transport but
provides no protection against connecting to an impersonating TLS server.

Automatic client restart preserves the complete fingerprint accepted during
an interactive confirmation. It never turns that value into a wildcard and
does not ask `y/N` again within the same automatic-restart chain.

## Configuration-file client

### frp-style TOML

```toml
serverAddr = "your-server"
serverPort = 7000
serverFingerprint = "SHA256:12:34:...:70:80"

[auth]
token = "change-me"

[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 6000
```

### Legacy frp INI

```ini
[common]
server_addr = your-server
server_port = 7000
token = change-me
server_fingerprint = SHA256:12:34:...:70:80

[ssh]
type = tcp
local_ip = 127.0.0.1
local_port = 22
remote_port = 6000
```

### rathole-style TOML

```toml
[client]
remote_addr = "your-server:2333"
default_token = "change-me"
server_fingerprint = "SHA256:12:34:...:70:80"

[client.services.ssh]
local_addr = "127.0.0.1:22"
```

Only TCP proxies are accepted. A client file may define multiple services. The
local endpoint can be the client itself or another TCP target reachable from
it. The wildcard syntax is identical in CLI, TOML, JSON, and INI values.
