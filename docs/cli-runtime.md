# Runtime and Restart CLI

This reference covers behavior shared by server and client processes: package
update detection, automatic restart, logging, exit status, and current limits.

## Runtime options

| Option | Default | Description |
| --- | --- | --- |
| `--auto-restart`, `--no-auto-restart` | enabled | Enable or disable installed-package monitoring and automatic restart |
| `--update-check-interval SECONDS` | `5.0` | Seconds between local distribution-version checks |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

The update interval must be greater than zero.

```bash
py-frp server --port-pool 6000-6009 --update-check-interval 2
py-frp client -c examples/frpc.toml --no-auto-restart
```

## What “update” means

The process reads the locally installed `py-simple-nat-tunnel` distribution
metadata. When that installed version differs from the code version already
loaded in memory, it restarts with:

```text
CURRENT_PYTHON -m py_frp ORIGINAL_ARGUMENTS...
```

The monitor does not contact PyPI and does not install anything. An operator,
deployment tool, or package manager must first change the package installed in
the same Python environment as the running command. See the
[deployment tutorial](tutorial.md#upgrade-a-running-server) for a complete
example.

“Same environment” also means the same import location. Python can have user,
system, virtual-environment, and editable installations visible at once. py-frp
checks only distribution metadata that points to the package directory actually
loaded by the process; an update installed into a shadowed copy is ignored.

## Server restart sequence

After detecting a changed installed version, the server performs these steps:

1. Stop the active serving coroutine.
2. Preserve the generated configless token and the current TLS certificate and
   private key in inherited restart state.
3. Send every control client a `server_restarting` message with
   `retry_after: 3.0`.
4. Close the control listener and connections, public listeners, pending
   tunnels, TLS context, and temporary certificate files.
5. Start the replacement command immediately, with no sleep or second update
   check after cleanup.

Client notification writes run concurrently and each has a one-second ceiling.
A dead or stalled peer therefore cannot block restart indefinitely.

Current clients wait the larger of the server's three-second suggestion and
their own `--reconnect-delay`. Older clients may ignore the unknown message,
but the following connection close still activates their ordinary reconnect
behavior.

## Supervisor and platform behavior

Linux, macOS, and Windows use the same two-process shape from the first launch:

```text
terminal -> persistent supervisor -> one server or client child
```

The supervisor owns the original terminal and remains alive while it replaces
the business child. It always keeps exactly one child: it waits for the old
child to finish cleanup, reaps it, and immediately starts the replacement with
the same Python executable, effective arguments, working directory, and normal
environment. Because the terminal-owning process never exits during a handoff,
the shell cannot print a prompt between the restart message and the new child's
output. POSIX `exec` is not used, and restart wrappers do not accumulate on any
supported platform.

The parent and child exchange only generation-tagged JSON records in a
private temporary directory. A stale child cannot issue a command for the next
generation. The child returns only py-frp's explicitly named compatibility
state; unrelated environment secrets are not serialized into the handoff.

Ctrl+C reaches both processes through their shared terminal; Windows
Ctrl+Break is normalized to the same path. The supervisor allows five seconds
for the child to run asyncio cleanup, then uses bounded terminate and kill
fallbacks if needed. Further console interrupts are ignored during that short
forced-cleanup section so they cannot strand the child. The supervisor returns
status 130 after the child is gone.

The same lifecycle is supported from Python 3.6 onward. A small internal
compatibility module maps missing Python 3.6 asyncio operations to their older
event-loop equivalents, provides the pre-3.7 TLS-minimum configuration, and
normalizes writer shutdown. These branches do not change the tunnel protocol or
the supervisor's one-parent/one-child invariant.

## Restart-loop protection

The supervisor carries the target version into the replacement and requires
the child to report the version it actually imported. A wrong-version child or
a child that exits during startup counts as a failed replacement. Three
failures toward the same target inside 30 seconds open the circuit breaker:
py-frp logs a warning, stops requesting that target, and keeps a stable
wrong-version child serving when one is available.

Suppression is scoped to the exact target version. It is not permanent: as soon
as installed metadata changes to another version, the supervisor clears the
old failure history and allows a new bounded three-attempt cycle. A successful
replacement clears its target's history immediately. This prevents inconsistent
metadata or a shadowed install from creating a restart storm and exhausting
processes, memory, handles, or disk-backed resources.

## Compatibility state across restart

Automatic restart intentionally preserves volatile state:

- A configless server reuses its generated pool token.
- The server restores the same certificate and private key, so its SHA-256 TLS
  fingerprint is byte-for-byte unchanged.
- A client that accepted a fingerprint interactively pins that complete value
  in the replacement process and does not ask `y/N` again.
- Original `--force`, `--priority`, connection, timeout, and configuration
  arguments remain in effect.

If an unpinned client detects an update before its first fingerprint decision,
it waits for that decision. Once the user confirms the fingerprint, the client
restarts immediately and transfers the accepted complete value. It never trusts
an unconfirmed server merely to finish an update restart.

Internal restart state passes through the private supervisor channel only to
the automatic replacement. It does not modify the parent shell. A later manual
launch is a new chain: a configless server creates a new token and TLS
certificate, while an unpinned client asks again.

`--no-auto-restart` disables update monitoring and the automatic state-transfer
path. The lightweight terminal-owning supervisor still launches the one
business child so signal and process ownership remain consistent on every
platform. Use the option when an external service manager deliberately owns the
entire update stop/start sequence.

## Exit status and logging

| Status | Meaning |
| --- | --- |
| `0` | Normal completion; also used after a logged fatal TLS, authentication, or pool-exhaustion response |
| `1` | Unhandled runtime or elevation failure |
| `2` | CLI parsing or configuration error |
| `130` | Interrupted with Ctrl+C |

Logs go to stderr. Generated pool credentials, TLS fingerprints, and assigned
pool ports go to stdout so automation can capture structured operational output
without mixing it with logs.

## Current limits

py-frp supports TCP reverse forwarding, multiple services, token
authentication, configless port pools, TLS fingerprint pinning and patterns,
and the documented configuration subsets. It does not currently support UDP,
HTTP/HTTPS virtual hosts, frp plugins, load balancing, STCP/XTCP, rathole wire
compatibility, or connection multiplexing. Each public TCP connection creates
one independent tunnel TCP connection.
