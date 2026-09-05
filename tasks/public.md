# Ledger: making tsdfm reachable by friends (public deploy)

Handoff doc for whoever picks this up next. Written mid-stream — this is config work,
**nothing here has been run against the real NAS yet.**

## Goal

Run tsdfm on the user's NAS (`theseus`, an existing Docker host reachable via a Docker
context over SSH) so friends can actually use it, using infrastructure that's already
running there: [Traefik](https://github.com/tsdaemon/theseus/tree/main/roles/services/traefik)
(docker-label-driven reverse proxy) and a Homepage dashboard. Local dev (`task dev`) is
unaffected and stays as-is.

## Current architecture (as configured, unverified live)

- `docker-compose.yml` (base) = Icecast + Liquidsoap only. Used directly by `task dev`
  locally, and as the base layer for deploy.
- `docker-compose.deploy.yml` (overlay) = adds the `app` service + labels. Deploy runs
  `docker --context theseus compose -f docker-compose.yml -f docker-compose.deploy.yml ...`
  (see `task deploy` / `deploy:down` / `deploy:logs` in `Taskfile.yml`) — this builds
  *on the remote daemon* over SSH, no registry needed.
- **Two separate paths reach `app` publicly vs. on the LAN, deliberately not the same one:**
  - LAN: Traefik's `web_local` entrypoint, plain HTTP, `Host(${APP_HOSTNAME_LOCAL})` — no
    TLS, matches how Traefik/Portainer route their own internal dashboards in the theseus
    role. Traefik reaches `app` via docker-socket discovery + the container's own network
    IP — no published port needed for this path.
  - Public: a **Cloudflare Tunnel** (`cloudflared`) — configured entirely outside this
    repo — terminates TLS at Cloudflare's edge and forwards straight to `app`'s published
    host port, bypassing Traefik entirely. This is why `app` in `docker-compose.deploy.yml`
    *also* publishes a real host port (`APP_PORT`, default 6490) despite Traefik not
    needing one — Cloudflare Tunnel has no docker-label discovery, it just needs an
    address:port to forward to.
  - Suggested (not yet confirmed set up on the Cloudflare side) ingress config: one public
    hostname, path-routed — `/radio.mp3` → Icecast's published port, everything else →
    app's published port. That makes the web UI and the audio stream look like one origin
    to browsers, matching how `ICECAST_STREAM_URL` gets embedded straight into the page.
- Icecast keeps a directly-published port (`ICECAST_PORT`, default 6491) rather than going
  through Traefik at all — the stream was already unauthenticated by design (invite token
  gates the *room*, not the raw mp3 URL), so a reverse proxy hop wouldn't add real security.
- Liquidsoap publishes **no** port in deploy (`ports: !reset []` overriding the base file's
  `127.0.0.1:1234:1234`, which exists only so `task dev`'s host-run app can reach it) —
  `app` reaches it by service name (`liquidsoap:1234`) over the compose network instead.
- `APP_PORT` (default 6490) and `ICECAST_PORT` (default 6491) are both configurable via
  `.env.deploy`, deliberately not the Dockerfile/icecast.xml defaults of 8080/8000 — purely
  to read as visually distinct from other services' ports on the NAS. Nothing actually
  collides either way (Docker namespaces per-container), this was an explicit ask, not a
  technical necessity. `ICECAST_PORT` had to be threaded through three places: Icecast's
  own `<listen-socket>` (`icecast/icecast.xml.template`, via `envsubst`), Liquidsoap's
  `output.icecast(port=...)` (`liquidsoap/radio.liq`), and the compose port mapping —
  changing it now only means setting the one env var.
- Homepage dashboard tile + `customapi` widget on `app` polls `GET /api/stats` (new
  endpoint, `app/src/tsdfm/main.py`) over the LAN-local Traefik route, showing live
  listener count (`len(active_sockets)`). Deliberately unauthenticated — a count isn't
  sensitive, and Homepage lives on the LAN anyway.
- Env files: `.env` (local dev, existing) vs. `.env.deploy` (new, gitignored, template at
  `.env.deploy.example`). `go-task` **merges** dotenv files rather than replacing — a
  per-task `dotenv:` only overrides keys it actually sets, so `.env.deploy` only needs to
  hold what's genuinely different for the NAS, not a full copy of every var.
- `INVITE_TOKEN` (local) vs. `DEPLOY_INVITE_TOKEN` (deploy) are **deliberately different
  env var keys** (mapped onto the container's `INVITE_TOKEN` in
  `docker-compose.deploy.yml`), not the same key left to fall through the dotenv merge —
  otherwise the local test room and the real deployment would silently share one invite
  link. `task invite` / `task deploy:invite` both call the same script
  (`app/src/tsdfm/print_invite.py`), parameterized via `TSDFM_ENV_PATH` /
  `TSDFM_INVITE_TOKEN_VAR` / `TSDFM_RESTART_HINT` env vars set per-task in `Taskfile.yml`
  — generating a fresh token prints a loud stderr banner naming the actual next command to
  run (`task deploy`), since docker doesn't hot-reload env vars into a running container.

## Open / unresolved — needs a decision, not yet acted on

1. **`APP_HOSTNAME` (external) is declared but not wired into anything.**
   `.env.deploy.example` has both `APP_HOSTNAME` (external/public domain) and
   `APP_HOSTNAME_LOCAL` (LAN domain, used by the Traefik rule) — the user added both
   deliberately, "there are two domains, one external and one internal". But right now
   every label in `docker-compose.deploy.yml` (Traefik rule, `homepage.href`,
   `homepage.icon`, `homepage.widget.url`) uses `APP_HOSTNAME_LOCAL` only.
   **Unanswered question put to the user:** should `homepage.href` (the link a human
   clicks from the dashboard) point at the external `APP_HOSTNAME` instead, so it works
   when checking the dashboard from outside the LAN? Or leave everything on
   `APP_HOSTNAME_LOCAL` for consistency? No reply yet.
2. **`ICECAST_STREAM_URL` in `.env.deploy.example` doesn't reflect the path-routing
   suggestion above.** It's still `http://your.server.hostname:6491/radio.mp3` (direct
   port style) rather than something like `${APP_PUBLIC_URL}/radio.mp3` (same public
   hostname as the app, Cloudflare routing by path). Whether to actually restructure it
   that way depends on how the Cloudflare Tunnel ends up configured — not decided.
3. **The Cloudflare Tunnel itself does not exist yet** (or if it does, wasn't touched this
   session) — `cloudflared` ingress rules, the tunnel, DNS records, all outside this repo
   and not verified. Nothing here can actually go live publicly until that's set up to
   forward to `app`'s and `icecast`'s published ports.

## Not yet done / verified

- **Nothing has been run against the real `theseus` host.** All `docker compose config`
  validation so far used `docker --context default` (this machine) against copies of the
  example env files, purely to check the YAML merges/interpolates correctly — never an
  actual `task deploy`.
- `.env.deploy` (the real file, not `.env.deploy.example`) doesn't exist yet — needs
  `cp .env.deploy.example .env.deploy` and real values filled in, including a fresh
  `DEPLOY_INVITE_TOKEN` via `task deploy:invite`.
- `docs/architecture.md` and `README.md` were kept in sync with each change as it
  happened, but haven't been re-read end-to-end for consistency after this many edits in
  one session — worth a full re-read before trusting them as documentation.

## Where things live

- `docker-compose.yml` / `docker-compose.deploy.yml` — the two compose files (see above).
- `.env.deploy.example` — template for the new deploy-only env file.
- `Taskfile.yml` — `deploy`, `deploy:down`, `deploy:logs`, `deploy:invite` tasks.
- `app/src/tsdfm/main.py` — `GET /api/stats` endpoint.
- `app/src/tsdfm/print_invite.py` — generalized to serve both `task invite` and
  `task deploy:invite` via env-var parameterization.
- `icecast/icecast.xml.template`, `liquidsoap/radio.liq` — `ICECAST_PORT` threaded through.
- `docs/architecture.md` (`## Deployment topologies`, `## Configuration`), `README.md`
  (`## Deploying`) — narrative docs, updated alongside the code each step.
