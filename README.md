# py-frp

A small Python TCP reverse tunnel for exposing a service behind NAT through a
public server.

## Quick Start

Install py-frp on both the public server and the private client:

```bash
pip install py-simple-nat-tunnel
```

Start the public server with the example configuration:

```bash
py-frp server -c examples/frps.toml
```

Start the private client and confirm that the TLS fingerprint matches the one
printed by the server:

```bash
py-frp client -c examples/frpc.toml
```

The example exposes the client's `127.0.0.1:22` as port `6000` on the public
server. Connect to it with:

```bash
ssh -p 6000 user@your-server
```

Replace `127.0.0.1`, the ports, and `change-me` in the example files before
using them outside a local test.

For every command, option, and supported configuration format, see the
[CLI manual](https://github.com/GGN-2015/py-frp/blob/main/docs/cli.md). The
[configless deployment tutorial](https://github.com/GGN-2015/py-frp/blob/main/docs/tutorial.md)
covers production setup and automatic update restart. For the tunnel lifecycle,
protocol, allocation, and security design, see the
[algorithm manual](https://github.com/GGN-2015/py-frp/blob/main/docs/algorithm.md).
