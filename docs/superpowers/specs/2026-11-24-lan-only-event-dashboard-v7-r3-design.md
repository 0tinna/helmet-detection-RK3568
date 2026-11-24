# LAN-only Event Dashboard v7-r3 Design

## Intent

Make the RK3568 no-hat evidence archive useful from a phone on the same
private LAN without SMTP, invitation URLs, cookies, or manual configuration.
Opening `http://BOARD_LAN_IP:8092/` must show both the current alert state
and traceable no-hat records with their saved image, time, event ID, frame ID,
count, and retention state.

The dashboard is read-only. It does not expose the live preview, camera,
detection controls, Wi-Fi controls, resident-app controls, public IP, or
SMTP state. `8091` keeps its existing live-preview role.

## User-visible Contract

- `/`, `/events`, `/api/events`, `/api/status`, `/healthz`, and
  `/events/<event-id>.jpg` are directly readable from the same private LAN.
- `/alerts` redirects to `/` for old bookmarks; it contains no token check.
- `event-access.key`, `mail.json`, SMTP helpers, invitation links, and access
  cookies are absent from fresh root files and release archives.
- Current alert state is shown independently from historical records:
  `active` is danger, `clear` is clear, `pending`/`cooldown` is an alert
  window, and invalid/idle/stale input is unknown or standby.
- An empty archive says no no-hat record was recorded. It never claims that
  the worksite is safe.
- Event cards show the annotated JPEG, Shanghai timestamp, event ID, frame ID,
  and number of visible bare-head tracks. The existing seven-day retention,
  256 MiB, 5,000-record, and 128 MiB free-space floors remain unchanged.

## Architecture

`AlertHub` owns the event store and optional `LanEventServer`. A fresh event
packet is still validated, tracked, and atomically written by
`AlertEventStore`. The HTTP server reads only the public event index and media
paths. `LocalService` continues to bind the controller status provider, but
the progress schema carries viewer cleanup state only.

The page is one inline HTML document using semantic sections, a skip link,
labelled date filtering, exact URL query state, truthful loading/empty/error
states, image dimensions and alt text, an `aria-live` connection announcement,
visible keyboard focus, and reduced-motion support. It has no external assets.

## Security Boundary

- The listener remains bound to `0.0.0.0:8092`.
- `_is_lan_client()` rejects non-loopback public IPv4 and non-IPv4 sources.
- The server exposes only GET reads; mutating methods return `405`.
- CSP keeps scripts, styles, and images self-only and disables framing.
- The server never returns absolute filesystem paths, public IP, credentials,
  account addresses, or exception text.
- Router/hotspot forwarding remains unsupported. LAN source filtering is a
  guardrail, not a replacement for a trusted Wi-Fi password or firewall.

## Approaches Considered

1. Direct LAN dashboard, chosen. It removes setup and notification plumbing,
   keeps the evidence path small, and matches the current single-board use.
2. Invite-cookie dashboard without mail, rejected. It preserves working auth
   code but leaves no simple way to distribute or rotate invitations.
3. Internet-hosted dashboard, rejected. It adds NAT, authentication, TLS,
   connectivity, and storage policy outside the requested offline/LAN scope.

## Acceptance

Host tests must prove direct HTTP and media reads, no credentials, no mail
state, LAN/public-source separation, read-only routes, query filtering, and the
existing event freshness/retention contracts. The v7-r3 package must contain no
SMTP or access-key files. Board acceptance must open `8092` from a same-LAN
phone, show current state plus a real recorded event, and retain truthful
unknown/empty states when no fresh valid event exists.
