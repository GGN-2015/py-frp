# CLI Manual

This page is the short index for py-frp's command line. The detailed reference
is split by task so that server operators and client users do not have to scan
options that do not apply to them.

## Install

```bash
pip install py-simple-nat-tunnel
```

For development from a checkout, use `pip install -e .`. Check the active
installation with:

```bash
py-frp --version
```

## Choose a reference

| I want to... | Read |
| --- | --- |
| Start or configure the public server | [Server CLI](cli-server.md) |
| Connect a private service, use `--force`, or choose `--priority` | [Client CLI](cli-client.md) |
| Control update detection, automatic restart, logging, or exit behavior | [Runtime and restart CLI](cli-runtime.md) |
| Deploy configless mode safely from start to finish | [Configless deployment tutorial](tutorial.md) |
| Understand allocation, preemption, TLS, and the wire protocol | [Algorithm manual](algorithm.md) |

## Entry points

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

All aliases normalize to the same implementation. Automatic restart preserves
the selected server/client role and the original effective arguments.

## Shared runtime options

The following options exist on both `server` and `client`:

| Option | Default | Meaning |
| --- | --- | --- |
| `--auto-restart`, `--no-auto-restart` | enabled | Enable or disable restart when the locally installed package version changes |
| `--update-check-interval SECONDS` | `5.0` | Interval between installed-version checks |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

The update interval must be greater than zero. Automatic restart is enabled by
default; it detects a package installed by another command but never contacts
PyPI or installs an update itself. See [Runtime and restart CLI](cli-runtime.md)
for the exact sequence and cross-platform behavior.

## Configuration-file scope

py-frp accepts a practical subset of frp TOML/INI and rathole TOML
configuration. This is configuration compatibility only; py-frp does not
implement either project's wire protocol. The exact accepted examples live in
the server and client references linked above.
