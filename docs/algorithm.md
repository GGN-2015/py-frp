# Algorithm Manual

This manual describes py-frp's runtime design and private wire protocol. The
protocol is native to py-frp; accepting frp and rathole configuration files does
not make it wire-compatible with either project.

## Topology

```text
public user          public server             private client        target
    |                     |                           |                  |
    | TCP to public port  |                           |                  |
    |-------------------->|                           |                  |
    |                     | control: open(id)         |                  |
    |                     |==========================>|                  |
    |                     |     new TLS tunnel(id)    | TCP to target    |
    |                     |<==========================|----------------->|
    |<------------------ framed, bidirectional relay ------------------->|
```

`=` denotes the TLS-protected server-to-client segment. A long-lived control
connection carries registration, heartbeat, and tunnel-opening messages. Each
public TCP connection creates a separate TLS tunnel connection.

## Startup and identity

1. The server generates a P-256 EC private key and a self-signed certificate.
   The certificate is valid for seven days, but both key and certificate live
   only in a temporary directory for the current server process.
2. The server configures a TLS listener with TLS 1.2 as the minimum and prints
   the certificate's SHA-256 fingerprint.
3. The client opens TLS without CA or hostname validation because the
   certificate is intentionally ephemeral. Before sending a token, it hashes
   the peer certificate and compares or interactively confirms the fingerprint.
4. Once confirmed, the client stores the normalized fingerprint in memory and
   requires every control and data connection in that process to match it.

Complete fingerprint comparisons normalize case and separators, then compare
all 32 bytes in constant time. A configured fingerprint may instead contain one
literal `...`, which matches zero or more complete bytes between an anchored
prefix and suffix. Fixed portions are also compared in constant time; using
fewer fixed bytes deliberately weakens the pin. A bare `...` matches every
fingerprint and logs a startup warning because it disables server identity
verification. A nonmatching certificate aborts the connection before
authentication data or tunnel traffic is sent.

## Installed-package update monitor

Both CLI runtimes start a lightweight asyncio task beside the server or client.
Every five seconds by default, it reads the installed distribution version with
`importlib.metadata`. The baseline is the version already loaded by the running
process. Missing metadata is treated as a temporary condition because package
installers may briefly remove old metadata before writing the replacement.
Only distribution metadata whose `py_frp` package directory resolves to the
directory of the code currently loaded in memory is considered. A newer package
installed in a shadowed system/user/virtual-environment location cannot trigger
a restart of a different loaded copy. If several metadata directories point to
the same loaded package directory, valid versions are parsed according to PEP
440 and the highest one is used.

The monitor deliberately does not contact PyPI and does not install an update.
Comparing a remote release without installing it would restart the same code in
a loop. An external package manager is responsible for changing the installed
distribution.

Update detection and process replacement are deliberately separate modules.
`update.py` owns installed-path selection, version comparison, and restart-loop
suppression. `restart.py` owns argument/environment continuity, POSIX `exec`,
the Windows child supervisor, and Ctrl+C cleanup. Neither module owns tunnel or
TLS state.

When the monitor detects a change, it follows this sequence:

1. For a client without an explicit fingerprint, wait until the current server
   fingerprint has been confirmed. An already connected or explicitly pinned
   client is ready immediately.
2. Cancel the active runtime task. Client cancellation closes its control and
   tunnel writers and waits for tracked tunnel tasks to finish.
3. Copy volatile restart state into internal inherited environment values.
4. On the server, broadcast a `server_restarting` control message to every
   connected control client. Message writes run concurrently and each has a
   one-second upper bound, so a stalled peer cannot indefinitely block restart.
5. Close the control listener and connections, public service listeners,
   pending tunnel futures, TLS context, and temporary certificate directory.
6. Immediately launch the current Python executable with `-m py_frp` and the
   original effective CLI arguments.

There is no delay after cleanup. On Linux and macOS, the POSIX `os.execv` call
replaces the process in place. On Windows, the first restart becomes one
foreground supervisor attached to the terminal. It launches one serving child
at a time. A later update makes that child return an internal restart code; the
same supervisor immediately reaps it and launches the replacement, so wrapper
processes do not nest across updates.

Ctrl+C reaches both Windows processes through their shared console. The
supervisor restores ordinary `KeyboardInterrupt` behavior after asyncio service
cleanup, waits up to five seconds for the child to exit cleanly, then uses
terminate and kill as bounded fallbacks. It exits with status 130. Standalone
entry points such as `py-frps` and `py-frpc` are normalized to the equivalent
`python -m py_frp server/client` form while preserving their arguments.

### Restart-loop circuit breaker

Before replacement, the target installed version is placed in inherited restart
state. The replacement compares that target with the version it actually
loaded. If they differ, it logs an error and refuses to restart toward that same
target again. It continues running the loaded version and waits for the matching
installation metadata to change to a different version. This circuit breaker
limits a failed handoff to one attempt even if metadata is inconsistent.

### Restart-state continuity

In configless pool mode, the first server process generates one random shared
token as usual. Before an automatic restart, the token is placed in an internal
environment value. The replacement process consumes that value instead of
calling the token generator, so client authentication remains compatible over
any number of consecutive automatic restarts.

The server also serializes its PEM certificate and matching EC private key into
base64 environment values before deleting the old temporary files. The new
process recreates fresh temporary files from the same material. Consequently,
the SHA-256 certificate fingerprint is byte-for-byte identical and pinned
clients accept the restarted server.

After an interactive client accepts a server fingerprint, it keeps the
normalized value in memory and copies it to inherited restart state immediately
before replacement. The replacement client treats that value exactly like a
configured pin, verifies every control and tunnel TLS connection against it,
and never repeats the `y/N` question for that automatic-restart chain. An
explicit CLI/config fingerprint takes precedence.

Internal restart state is inherited by POSIX `exec` or by Windows supervisor
children, but is not written back to the parent shell. It therefore survives
automatic replacements but not a later manual launch. Invalid, incomplete, or
mismatched preserved TLS material is a fatal security error rather than a reason
to generate a different fingerprint. `--no-auto-restart` omits the monitor and
this state-transfer path entirely.

## Control registration

Control messages are compact UTF-8 JSON objects terminated by a newline. A
message may not exceed 1 MiB.

The normal handshake is:

```text
client -> server  {"type":"hello","version":2}
server -> client  {"type":"hello","version":2,"server":"py-frp"}
client -> server  {"type":"register","services":[...]}
server -> client  {"type":"registered","status":"ok",...}
```

The server also accepts registration as the first message for compatibility
with older py-frp clients. A service registration contains its name and token,
plus a public host/port for dynamic services.

For a preconfigured service, the server ignores client-supplied bind details
and uses its own service definition. For a dynamic service, the server requires
`allow_dynamic`, validates the default token, and binds the requested public
address. Service names must be unique among connected clients. Registration is
transactional: if one service fails, listeners created earlier in the same
request are removed.

After registration, the client sends `ping` messages at the configured
heartbeat interval and the server responds with `pong`. Loss of the control
connection removes all of that client's public listeners and pending tunnels.
Recoverable failures cause the client to wait `reconnect_delay` and register
again. Fatal authentication or unforceable resource-exhaustion responses stop
the client.

### Coordinated server restart

Immediately before an automatic package-update shutdown, the server sends each
control client:

```json
{"type":"server_restarting","reason":"package_update","retry_after":3.0}
```

The notice is ordered before the server closes client writers. A current client
treats it as a recoverable, restart-specific disconnect and waits
`max(reconnect_delay, retry_after)` seconds before opening a new control
connection. Boolean, non-numeric, non-finite, zero, and negative suggestions
are ignored, leaving the locally configured reconnect delay in force. This
means an operator can choose a longer delay, while a server can supply a safe
minimum for a routine restart.

The server snapshots active control sessions under its state lock, but writes
the notice outside the lock. Notification failures are isolated per client;
cleanup and restart continue even if a peer has already vanished or stopped
reading. An older client may ignore the unknown message, after which server-side
connection closure still activates that client's ordinary reconnect policy.

## Port-pool allocation

Configless server mode parses every `--port-pool` value into an ordered,
duplicate-free set of valid ports. Inclusive ranges such as `6000-6009` and
comma-separated items are supported. Descending ranges and ports outside
1-65535 are rejected.

One shared token is generated with the operating system's cryptographic random
source. Its alphabet excludes `I`, `O`, `0`, `1`, and lowercase `l` to reduce
copying mistakes. The client derives its registration name from the first 16
hexadecimal characters of `SHA-256(token)`; the server replaces that name with
a unique per-session name after authentication.

Allocation runs under the server state lock:

1. Collect ports currently assigned to connected pool clients.
2. Examine configured ports in ascending numeric order.
3. Skip assigned ports and ports that the operating system refuses to bind.
4. Bind the first available port and associate it with the client session.
5. Return the actual public address in the `registered` response.

This makes allocation deterministic while allowing multiple simultaneous
clients to share one token. When the client disconnects, its listener closes and
the port returns to the pool.

If allocation fails because all usable ports belong to online pool clients, a
normal registration receives a `force_required` response. The client may close
the connection to decline or retry the same registration with `force: true`.
An initially forced registration, such as one produced by `--force`, skips the
question. Under the state lock, the server then:

1. Parse the incoming integer priority `N`; an omitted field is `0` for backward
   compatibility.
2. Keep only existing pool services with `priority >= N`. Lower numeric values
   are higher priority, so services with `priority < N` are protected.
3. If no service remains, reject the forced registration with a fatal error.
   The response includes `max_priority`, the largest number among all existing
   pool clients.
4. Among eligible services, find the largest priority number. If more than one
   has that number, select the client session with the earliest monotonic
   connection-creation timestamp.
5. Remove every service and pending tunnel owned by that client, close its
   public listener, and bind the newly freed port for the requester.
6. Send the victim a non-fatal `preempted` error containing both priorities,
   then close its control channel.

In compact form, the victim ordering key is: worst eligible numeric priority
first, then oldest connection first. Priority affects only the forced,
pool-full branch. Free-port allocation remains lowest-port-first, and a
non-forced registration never disconnects another client.

Closing the victim's control channel activates the ordinary client reconnect
loop. A reconnect performs a fresh registration, so it can receive another
`force_required` response; the interactive decision or persistent `--force`
policy and the original priority are applied each time. Allocation and victim
selection are serialized by the server state lock. Ports occupied by unrelated
operating-system processes do not create a force option because disconnecting a
py-frp client cannot free them.

## Opening a tunnel

When a public user connects to a registered service, the server performs these
steps:

1. Generate an unguessable URL-safe tunnel ID.
2. Store a pending record containing the tunnel ID, service, expected token,
   owning client, and a future used for pairing.
3. Send `{"type":"open","id":...,"service":...}` over the owning client's
   control connection.
4. Wait up to `open_timeout` (15 seconds by default) for the matching tunnel.
5. Once paired, relay bytes in both directions until both sides finish or an
   error occurs.

The client handles each `open` concurrently. It first attempts the configured
local target with `connect_timeout`, then opens a new TLS connection to the
server and sends a `tunnel` message containing the ID, service name, and token.
The server accepts it only when all three match a live pending record. An
incorrect token cannot consume the pending record, so a valid tunnel can still
claim it before timeout.

If the local target cannot be opened, the client still reports the tunnel
attempt and then closes it; the public connection consequently ends. Tunnel
tasks are tracked independently, allowing many public connections to be served
at once without blocking the control loop.

## Data framing and relay

After pairing, the TLS tunnel changes from JSON messages to binary frames:

```text
+------------+----------------------+-------------------+
| type: 1 B  | length: 4 B, big-end | payload: length B |
+------------+----------------------+-------------------+
```

| Type | Length | Meaning |
| --- | --- | --- |
| `D` | 0-65536 | Data bytes follow |
| `E` | 0 | End of this direction; no payload |

Each relay direction runs in its own asyncio task. Reads use 64 KiB chunks and
`drain()` provides transport backpressure. A plain-side EOF becomes an `E`
frame so half-close semantics can cross TLS, where direct `write_eof()` is not
available. Invalid types, oversized frames, truncated frames, or a tunnel that
closes without an EOF frame terminate the relay.

## Concurrency and cleanup

Mutable service and pending-tunnel maps are protected by one asyncio lock.
Writes to a shared control connection use a per-client write lock so heartbeat
and `open` JSON messages cannot interleave. Network relay occurs outside the
state lock.

Cleanup is ownership-based:

- A lost control session closes every public listener registered by that client.
- Pending tunnels owned by that client fail immediately.
- A timed-out or completed public connection removes its pending ID and closes
  both writers.
- Server shutdown closes the control listener, fails pending futures, closes
  service listeners, and removes the ephemeral certificate files.

## Security boundary

TLS and fingerprint pinning protect confidentiality and server identity only
between py-frp server and client. Token checks authorize service registration
and bind each data tunnel to its pending public connection. Tokens should still
be treated as secrets.

The public-user-to-server and client-to-target legs are outside this encryption
boundary. py-frp also has no multiplexing: resource use grows roughly with the
number of concurrent public connections because every connection owns a public
socket, a client-side target socket, and a dedicated TLS tunnel socket.
