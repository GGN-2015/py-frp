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

Fingerprint comparisons normalize case and separators, then use a
constant-time comparison. A different certificate aborts the connection before
authentication data or tunnel traffic is sent.

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

1. Selects the earliest registered pool service.
2. Removes every service and pending tunnel owned by that client.
3. Closes its public listener and binds the newly freed port for the requester.
4. Sends a non-fatal `preempted` error and closes the victim's control channel.

Closing the victim's control channel activates the ordinary client reconnect
loop. A reconnect performs a fresh registration, so it can receive another
`force_required` response; the interactive decision or persistent `--force`
policy is applied each time. Allocation and victim selection are serialized by
the server state lock. Ports occupied by unrelated operating-system processes
do not create a force option because disconnecting a py-frp client cannot free
them.

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
