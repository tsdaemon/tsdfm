# Backlog: ~7 s gap between UI "now playing" and audible track change (prod only)

Filed 2026-09-05 from a debugging session. Diagnosis is done; no code touched.
Nothing here is urgent — it's a polish/latency issue, the room works.

## Symptom

In the deployed room, a new song is *heard* ~7 seconds after the UI has already
swapped to it (title, art, progress bar, countdown). Consistent ~7 s. Not
reproducible to the same degree locally (`task dev` is ~1–2 s).

## Root cause

Two channels carry "a new track started", with very different latency, and
nothing reconciles them:

| Channel | Path | Latency |
|---|---|---|
| UI update (`on_air` / `progress`) | Liquidsoap telnet → app → WebSocket → browser | ~100 ms |
| The actual audio | Liquidsoap encoder → Icecast → **Cloudflare Tunnel/edge** → `<audio>` buffer | ~7 s in prod |

- The server flips `on_air` the instant the pushed request leaves Liquidsoap's
  pending queue (`app/src/tsdfm/state.py` ~L344, `pending_rids()` in
  `liquidsoap_control.py`). That's the *source* moment, pushed over the
  WebSocket essentially unbuffered.
- The audio for that same moment is still in the encode buffer + Icecast burst +
  **Cloudflare's read-ahead buffer** + the browser media buffer.
- Local baseline: Icecast `burst-size 65535` at 320 kbps ≈ 1.6 s, plus a second
  or two of `<audio>` buffer. The extra ~5 s in prod is the Cloudflare proxy
  layer buffering a streaming response. Per `tasks/done/public.md`, `/radio.mp3`
  is path-routed through the Cloudflare Tunnel to Icecast's published port, so
  the CF edge is in the audio path. Response buffering is only disableable on
  CF Enterprise; you can't un-proxy a Tunnel hostname.

`resyncToLiveEdge()` in `app/src/tsdfm/static/app.js` (~L957) cannot fix this: it
only measures the browser's *own* buffer (`buffered.end - currentTime`), is blind
to whatever CF holds, tolerates `LIVE_LAG_TOLERANCE = 5` s anyway, and runs after
`on_air` is already rendered.

## Options (roughly by impact / effort)

1. **Take `/radio.mp3` off the Cloudflare edge.** Kills most of the 5 s, no
   client complexity. User does *not* want to open an inbound port on the home
   router, so the realistic sub-options are:
   - **Tailscale Funnel** for the stream hostname only (NAS dials out, no port;
     thin TLS/TCP proxy through TS relays, adds tens of ms not seconds;
     `*.ts.net` hostname is fine for a private room). Check TS is already on
     theseus.
   - **Cheap VPS as front door**: $3–5/mo box holds the public IP + open port,
     NAS connects out over WireGuard/Tailscale, VPS nginx proxies the stream back
     down the tunnel. Own domain, full control, nothing inbound at home.
   - Keep the app + WebSocket behind Cloudflare unchanged — point only the audio
     at the new path. It's one env var: `ICECAST_STREAM_URL`
     (`app/src/tsdfm/main.py` L64 → injected into `index.html`).
   - **Auth wrinkle**: stream auth is cookie-based today — Icecast forwards the
     `Cookie` header to `POST /api/stream-auth` (`main.py` ~L130); the session
     cookie is set for the app origin (`main.py` ~L124). A different stream
     hostname → browser won't send that cookie. Fix: put a signed token on the
     stream URL (`?t=…`) that `stream-auth` validates instead of / in addition to
     the cookie — it already parses the mount query string, small change.

2. **Make the UI honest instead** (if CF stays). Client receives next-track
   metadata over WS but holds it as `pendingNowPlaying`; applies it after a
   measured offset so the swap lines up with the ear. Progress bar / countdown
   become accurate too. Doesn't remove latency, just stops the UI lying.
   - Simple version: one calibration at join (or every few min) to estimate the
     offset; delay UI swaps by it. ~20 lines, no Web Audio.

3. **Acoustic marker** (proper per-listener exact sync, heaviest). Liquidsoap
   mixes a short coded ~18–19 kHz burst on each `on_track`; client taps the
   element via Web Audio (`createMediaElementSource` → `AnalyserNode` /
   `AudioWorklet` FFT), swaps `pendingNowPlaying` when the marker is detected.
   Needs CORS on the stream + `crossorigin="anonymous"`, a coded pattern (not a
   single tone) to avoid music false-triggers, and a fallback timer for
   muted/failed-detection listeners. Broadcasters do this for companion apps;
   it's more than this room needs unless option 1 is off the table.

4. **Minor, stacks with the above**: `burst-on-connect 0` in
   `icecast/icecast.xml.template` (only helps reconnects, ~1.6 s at 320 kbps);
   drop `LIVE_LAG_TOLERANCE` and force `radioAudio.load()` + play on every track
   change (small glitch per song boundary, CF buffer still caps how close "live
   edge" gets).

## Recommendation

Option 1 via Tailscale Funnel (or VPS relay) is the clean fix and matches the
"no inbound port" constraint. Option 2-simple is the cheap fallback if the
network change stays un-fun.

## Files in play

- `app/src/tsdfm/static/app.js` — `syncAudio` / `resyncToLiveEdge` / `LIVE_LAG_TOLERANCE`, the `progress` + `state` message handlers.
- `app/src/tsdfm/state.py` — `on_air` / `remaining` computation (~L344), `progress` broadcast (~L361).
- `app/src/tsdfm/liquidsoap_control.py` — `pending_rids()`, `remaining()`.
- `app/src/tsdfm/main.py` — `ICECAST_STREAM_URL` (L64), `POST /api/stream-auth` (~L130), session cookie (~L124).
- `icecast/icecast.xml.template` — `burst-on-connect` / `burst-size`.
- `tasks/done/public.md` — deploy topology, Cloudflare Tunnel path routing.
