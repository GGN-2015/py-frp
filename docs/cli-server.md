# Server CLI

Use the server command on the public machine. It owns the TLS control listener
and the public ports that users connect to.

```text
py-frp server [-c PATH | --port-pool PORTS] [options]
```

The server requires either a configuration file or at least one `--port-pool`.

## Options

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
| `--auto-restart`, `--no-auto-restart` | on | Enable or disable restart after an installed package version change |
| `--update-check-interval SECONDS` | `5.0` | Seconds between installed package version checks |
| `--log-level LEVEL` | `INFO` | Logging threshold |

## Configless port-pool mode

Start a control listener on port 7000 and offer public ports 6000 through 6009:

```bash
py-frp server --bind-port 7000 --port-pool 6000-6009
```

`--port-pool` may be repeated, and each occurrence may contain comma-separated
ports or ranges:

```bash
py-frp server --port-pool 6000-6009,7000 --port-pool 7100-7102
```

Ranges are inclusive. Duplicate ports are removed. A descending range or a
port outside 1-65535 is rejected before startup.

On startup the server prints its TLS fingerprint and one generated shared pool
token:

```text
tls_fingerprint SHA256:12:34:56:78:90:AB:CD:EF:10:32:54:76:98:BA:DC:FE:01:23:45:67:89:AB:CD:EF:10:20:30:40:50:60:70:80
py-frp token pool
control 0.0.0.0:7000
public_bind 0.0.0.0
token GENERATED_TOKEN
```

Give the token only to authorized clients. Distribute the fingerprint through
a trusted channel. The token authenticates pool registration; the fingerprint
lets clients verify that they reached the intended server before sending it.

Each connected pool client receives the lowest usable free port. A port already
occupied by another process is skipped. When every usable port belongs to an
online client, the server offers preemption only when the incoming client makes
a forced request. Victim selection and priority rules are documented in the
[Client CLI](cli-client.md#forced-connection-and-priority).

For a production-oriented walkthrough covering firewall rules, token handling,
fingerprint pinning, priorities, and upgrades, use the
[configless deployment tutorial](tutorial.md).

## Configuration-file mode

### frp-style server

This minimal TOML enables dynamically registered TCP services:

```toml
bindPort = 7000

[auth]
token = "change-me"
```

The server accepts `.ini` and `.conf` as legacy frp INI. A `.json` suffix is
parsed as JSON; every other suffix is parsed as TOML. Supported settings include
the control bind address and port, default token, open timeout, dynamic-service
policy, and preconfigured TCP services.

### rathole-style server

A rathole-style server file predefines services and disables dynamic services
by default:

```toml
[server]
bind_addr = "0.0.0.0:2333"
default_token = "change-me"

[server.services.ssh]
bind_addr = "0.0.0.0:6000"
```

Configuration compatibility does not imply frp or rathole wire compatibility;
both peers must run py-frp.

## Privileged ports

When any configured control, service, or pool listener uses a port below 1024,
the server attempts one relaunch through `py-admin-launch`:

- `--elevate` requests elevation explicitly.
- `--no-auto-elevate` disables automatic elevation for low ports.
- The one-attempt marker prevents an elevation loop.

Prefer an unprivileged port or an operating-system service account with only
the capabilities it needs.

## Automatic restart

Automatic update restart is enabled by default. A configless server preserves
its generated token and TLS certificate across an automatic restart, notifies
clients before disconnecting them, closes all listeners, and immediately starts
the replacement command. See [Runtime and restart CLI](cli-runtime.md) for the
complete sequence and platform differences.
