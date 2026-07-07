# py-frp

A small Python TCP reverse tunnel inspired by the public-server/private-client
model used by frp and rathole. The current scope is TCP port forwarding with a
server, a client, a practical subset of frp TOML/INI configs, and a practical
subset of rathole TOML configs.

## Installation

Install from PyPI:

```bash
pip install py-simple-nat-tunnel
```

Install from source:

```bash
pip install .
```

Install in editable development mode:

```bash
pip install -e .
```

Installed commands:

```bash
py-frp --version
py-frp server -c examples/frps.toml
py-frp client -c examples/frpc.toml
py-frps -c examples/frps.toml
py-frpc -c examples/frpc.toml
```

On Windows, if the scripts directory is not on `PATH`, use the module entry
point directly:

```bash
python -m py_frp server -c examples/frps.toml
python -m py_frp client -c examples/frpc.toml
```

## Quick Start

On the public server:

```bash
py-frp server -c examples/frps.toml
```

On the private client:

```bash
py-frp client -c examples/frpc.toml
```

The default `examples/frpc.toml` exposes the client's `127.0.0.1:22` on the
server's `0.0.0.0:6000`. Connect from outside with:

```bash
ssh -p 6000 user@your-server
```

## Configless Port Pool Mode

The server can run without a config file by specifying a control port and a
public port pool:

```bash
py-frp server --bind-port 7000 --port-pool 6000-6009
```

`--port-pool` can be repeated. The server merges all ranges and single ports;
`a-b` means the inclusive range from `a` through `b`, and `a` by itself adds one
port:

```bash
py-frp server --bind-port 7000 --port-pool 6000-6009 --port-pool 7000 --port-pool 7100-7102
```

At startup, the server prints one shared random token to stdout. Tokens use
only unambiguous letters and digits, excluding `I`, `O`, `0`, `1`, and `l`.
Give that one token to all clients that are allowed to use the pool.

Clients only need the server address and the shared token. The local target
defaults to `127.0.0.1:22`:

```bash
py-frp client --server your-server:7000 --token TOKEN_FROM_SERVER
```

After registration, the client prints the assigned remote port to stdout:

```text
6003
```

If the local target is not SSH, set it explicitly:

```bash
py-frp client --server your-server:7000 --token TOKEN_FROM_SERVER --local 127.0.0.1:8080
```

Multiple clients can use the same shared token at the same time. Each client is
assigned one currently unused port from the pool, using the lowest available
port first. If the pool is exhausted, the server rejects the new client and the
client logs a single insufficient-resource error instead of disconnecting an
existing client.

## frp Config Subset

Server `frps.toml`:

```toml
bindPort = 7000

[auth]
token = "change-me"
```

Client `frpc.toml`:

```toml
serverAddr = "your-server"
serverPort = 7000

[auth]
token = "change-me"

[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 6000
```

Legacy frp INI `[common]` and TCP proxy sections are also supported:

```ini
[common]
server_addr = your-server
server_port = 7000
token = change-me

[ssh]
type = tcp
local_ip = 127.0.0.1
local_port = 22
remote_port = 6000
```

## rathole Config Subset

Server:

```toml
[server]
bind_addr = "0.0.0.0:2333"
default_token = "change-me"

[server.services.ssh]
bind_addr = "0.0.0.0:6000"
```

Client:

```toml
[client]
remote_addr = "your-server:2333"
default_token = "change-me"

[client.services.ssh]
local_addr = "127.0.0.1:22"
```

## Privilege Elevation

When the server needs to bind a privileged listen port below 1024, it tries to
relaunch once through `py-admin-launch` by default. You can also control this
explicitly:

```bash
py-frp server -c frps.toml --elevate
py-frp server -c frps.toml --no-auto-elevate
```

The relaunched command receives an internal marker so elevation is attempted at
most once.

## Current Scope

Supported:

- TCP reverse port forwarding
- Multiple services/proxies
- Token authentication
- Configless shared-token port pool mode
- Common frp TOML and legacy INI TCP configs
- Common rathole TOML TCP configs
- One-command installation through `pip install`
- Console scripts

Not currently supported:

- UDP
- HTTP/HTTPS virtual hosts
- frp plugins, load balancing, STCP, or XTCP
- rathole Noise/TLS encrypted transport
- Multiplexing; each public connection opens one independent tunnel TCP

## Tests

```bash
python -m unittest discover -s tests -v
```
