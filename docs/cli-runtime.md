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

## Platform behavior

| Platform | Mechanism |
| --- | --- |
| Linux and macOS | POSIX `exec` replaces the cleaned-up process in place |
| Windows | One foreground supervisor rotates one serving child at a time in the same terminal |

Both mechanisms retain the Python executable, effective arguments, working
directory, environment, and terminal. The replacement begins immediately after
cleanup. Windows uses a foreground supervisor because Windows `exec` behavior
does not provide the same reliable terminal-attached replacement semantics as
POSIX. Later updates return an internal code to that same supervisor, which
reaps the old child before starting the next one; wrappers never accumulate.

On Ctrl+C, the Windows supervisor allows five seconds for the serving child to
run its asyncio cleanup. It then terminates, and finally kills, a child that
does not stop within bounded time. The supervisor exits with status 130. On
Linux and macOS, Ctrl+C is handled directly by the exec-replaced process.

## Restart-loop protection

The target version is carried into the replacement. If the new process loads a
different version, py-frp logs an error and suppresses every further restart
toward that same target while continuing to serve with the loaded code. A later
change to a different installed version may trigger one new attempt. This
prevents inconsistent metadata or a shadowed install from creating a restart
storm and exhausting processes, memory, handles, or disk-backed resources.

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

Internal restart state is inherited only by the automatic replacement. It does
not modify the parent shell. A later manual launch is a new chain: a configless
server creates a new token and TLS certificate, while an unpinned client asks
again.

`--no-auto-restart` disables both monitoring and this automatic state-transfer
path. It is appropriate when an external service manager deliberately owns the
entire stop/start sequence.

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
