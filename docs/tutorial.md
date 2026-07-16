# Configless Deployment and Automatic-Restart Tutorial

This tutorial builds a configless TCP tunnel, turns it into a predictable
unattended deployment, and then upgrades the running server without changing
its token or TLS identity.

## 1. Plan the topology

Assume:

- `public.example.com` is reachable from the Internet.
- TCP 7000 is the py-frp control port.
- TCP 6000-6004 is the public port pool.
- The private client can reach a target at `127.0.0.1:22`.

Open TCP 7000 and 6000-6004 in the public host firewall and any cloud security
group. Do not expose the private target directly.

Install py-frp in a dedicated virtual environment on both machines. A virtual
environment makes it unambiguous which Python installation the update monitor
is observing:

```bash
python -m venv .venv
```

Activate it using the normal command for your shell, then install:

```bash
python -m pip install py-simple-nat-tunnel
py-frp --version
```

Use compatible py-frp versions on both sides. py-frp accepts some frp and
rathole configuration formats, but it does not speak their wire protocols.

## 2. Start the configless server

On the public host:

```bash
py-frp server \
  --bind-host 0.0.0.0 \
  --bind-port 7000 \
  --port-pool 6000-6004
```

The server prints values similar to:

```text
tls_fingerprint SHA256:12:34:56:78:90:AB:CD:EF:10:32:54:76:98:BA:DC:FE:01:23:45:67:89:AB:CD:EF:10:20:30:40:50:60:70:80
py-frp token pool
control 0.0.0.0:7000
public_bind 0.0.0.0
token GENERATED_TOKEN
```

Handle these two values differently:

- Treat `GENERATED_TOKEN` as a password. Transfer it only to authorized client
  operators and avoid putting it in shell history or public logs.
- Transfer the complete TLS fingerprint over a separate trusted channel. It is
  not secret; its job is to let the client detect an impersonated server.

The generated token and certificate survive automatic package-update restarts.
A later manual start, machine reboot, or fresh process chain generates new
values. If credentials must survive those events without redistribution, use a
configuration file instead of configless mode.

## 3. Make the first client connection

On the private host, first connect interactively:

```bash
py-frp client \
  --server public.example.com:7000 \
  --token GENERATED_TOKEN \
  --local 127.0.0.1:22
```

The client prints the server fingerprint and asks whether to trust it. Compare
all 32 bytes with the trusted server output, then answer `y`. When registration
succeeds, the client prints its assigned public port, such as:

```text
6000
```

Test the public route from another machine:

```bash
ssh -p 6000 user@public.example.com
```

The public-to-server and client-to-target legs are separate plain TCP segments.
Use an end-to-end encrypted application protocol such as SSH or HTTPS when the
payload is sensitive.

## 4. Convert the client to an unattended command

For an unattended client, supply the complete fingerprint so startup never
waits for input:

```bash
py-frp client \
  --server public.example.com:7000 \
  --token GENERATED_TOKEN \
  --server-fingerprint 'SHA256:12:34:56:78:90:AB:CD:EF:10:32:54:76:98:BA:DC:FE:01:23:45:67:89:AB:CD:EF:10:20:30:40:50:60:70:80' \
  --local 127.0.0.1:22 \
  --reconnect-delay 5
```

The literal `...` is also supported as a partial fingerprint wildcard. For
example, `SHA256:12:34:...:70:80` matches any 32-byte fingerprint with that
prefix and suffix. This is weaker than a complete pin: use it only when a
partial pin is an explicit operational requirement. The exact grammar and
security tradeoff are in the
[client fingerprint reference](cli-client.md#server-fingerprint-verification).

## 5. Choose a preemption policy

By default, a full server asks the client whether it may evict another pool
client. This prompt is appropriate for a person watching the terminal. For an
unattended process, decide in advance whether it may preempt:

```bash
py-frp client \
  --server public.example.com:7000 \
  --token GENERATED_TOKEN \
  --server-fingerprint 'FULL_SERVER_FINGERPRINT' \
  --local 127.0.0.1:22 \
  --force \
  --priority 0
```

Smaller priority numbers are stronger. A simple site policy is:

| Workload | Suggested priority | Meaning |
| --- | ---: | --- |
| Critical administration | `-10` | Protected from ordinary `0` and opportunistic clients |
| Normal service | `0` | Default |
| Disposable or batch access | `10` | First group selected for eviction |

A forced client can evict only clients with an equal or larger number. Among
those, the server selects the largest number, then the oldest connection. If a
priority-10 client arrives when every existing client is priority 0, it is
rejected and told that the current maximum is 0.

Use `--force` only when repeated preemption is acceptable. A disconnected
forced client keeps the same arguments when reconnecting and may compete again.
Provision enough pool ports and use nontrivial reconnect delays to avoid a
cycle of clients continuously evicting one another.

## 6. Understand the default update behavior

Both server and client enable automatic restart by default. Every five seconds
they compare:

- the package version loaded in the running process; and
- the version in the locally installed distribution metadata.

They do not query PyPI and do not run `pip install`. Therefore “automatic
restart” is not “automatic download”: your deployment process changes the
installed package, then py-frp notices and restarts itself.

Change the detection interval only when necessary:

```bash
py-frp server --port-pool 6000-6004 --update-check-interval 2
```

Keep the default enabled for the built-in handoff. Use `--no-auto-restart` only
when an external supervisor deliberately performs the whole restart and state
continuity is not needed.

## 7. Upgrade a running server

Leave the server running. In a second administrative terminal, use the Python
from the same environment that launched it:

```bash
python -m pip install --upgrade py-simple-nat-tunnel
```

The following handoff then happens automatically:

1. The running server detects the changed installed version.
2. It saves the generated token and exact TLS certificate/key for its
   replacement.
3. It sends connected clients `server_restarting` with a three-second minimum
   retry suggestion.
4. It closes every public and control listener and all pending tunnels.
5. Immediately after cleanup, it starts the same command with the same Python,
   arguments, working directory, environment, and terminal.

There is no deliberate service gap after cleanup. Linux and macOS replace the
process with POSIX `exec`. Windows starts the new Python process in the
foreground in the same terminal while the cleaned-up old process remains only
as its wrapper.

The replacement server prints the same token and the same SHA-256 fingerprint.
Clients that received the restart notice wait
`max(reconnect-delay, 3 seconds)` and then reconnect. A configless `--force`
client retains its force choice and priority during that reconnect.

This is the important validation checklist after an upgrade:

- the replacement logs appear after the restart line;
- the control listener is bound again;
- the printed token is unchanged;
- the printed TLS fingerprint is unchanged;
- clients reconnect after their backoff and receive service ports again.

## 8. Upgrade a running client

Install the new package into the client's own Python environment in the same
way. The client closes its control and tunnel resources, then restarts with the
same command.

If the client originally confirmed the server interactively, it transfers the
complete confirmed fingerprint to its replacement. The new process verifies
that exact fingerprint and does not ask `y/N` again. If an update is detected
before the first fingerprint decision, restart waits until the user has made
that decision; an unconfirmed certificate is never silently trusted.

## 9. Operational checklist

- Reserve the control and pool ports so unrelated processes cannot bind them.
- Permit only the required firewall ranges.
- Keep the generated token out of logs and command histories where possible.
- Prefer a complete 32-byte server fingerprint over a wildcard pattern.
- Give critical clients smaller priority numbers and discretionary clients
  larger numbers.
- Use `--force` only for clients that are genuinely allowed to evict others.
- Run upgrades with the exact Python environment that owns the running py-frp
  installation.
- Monitor stderr logs and the stdout values that automation depends on.
- For state that must survive machine reboot or manual restart, graduate from
  configless mode to an explicit server/client configuration.

For individual flags, use the [server CLI](cli-server.md),
[client CLI](cli-client.md), and [runtime/restart CLI](cli-runtime.md). For the
wire-level reasoning behind the behavior, see the [algorithm manual](algorithm.md).
