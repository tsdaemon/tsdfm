# tsdfm

A self-hosted listening room for a handful of friends. Everyone takes turns as DJ,
queueing tracks from your own Navidrome library, and everyone hears the same broadcast at
the same moment — like turntable.fm, except it's your server and your music.

It's a real internet radio station under the hood, so "in sync" isn't a clever client-side
trick: there is one live stream and browsers tune into it.

## What it does

- **DJ rotation** — each person has their own queue; playback round-robins between DJs
  rather than draining one person's list first
- **Genuinely synced** — one Icecast broadcast, so no drift between listeners
- **Live chat** alongside the player
- **Vote to skip** (majority of people in the room) and a like button
- **DJ breaks** (optional) — a toggle in the UI puts a short AI-voiced line between songs,
  like a radio host with a joke or a fun fact about the last track, and it can react to
  recent chat or welcome someone who just joined. Script via OpenRouter (rotates models
  so you can compare them), voice via a local Piper container — no per-use cost. Off
  unless you set `OPENROUTER_API_KEY`; while it's on, recent chat and names go to the
  script model (see `.env.example`).
- **Search your own library** — Navidrome/Subsonic, including cover art
- **Invite by link** — one shared link, no accounts, no signup
- **A log panel in the UI** so you can see what's failing without SSHing anywhere

## Requirements

- Docker + Docker Compose
- [go-task](https://taskfile.dev) for the commands below
- An existing **Navidrome** instance with your music (this stack does not run or manage it)
- [uv](https://docs.astral.sh/uv/) to run the app itself (it never runs in local Docker —
  see [Running it](#running-it) below)

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

- Point `NAVIDROME_*` at your instance. A dedicated Navidrome user for this app is a good
  idea, since its credentials sit in `.env`.
- Set the three `ICECAST_*` passwords to anything random.
- Set `ICECAST_HOSTNAME` and `ICECAST_STREAM_URL` to the address your **friends** will
  reach — a LAN IP, a domain, or a Tailscale/Cloudflare Tunnel hostname. `localhost` works
  only for you.
- Set `APP_PUBLIC_URL` the same way, so invite links print correctly.
- Leave `INVITE_TOKEN` blank; it gets generated on first `task invite`.

## Running it

```bash
task dev       # icecast + liquidsoap in docker, app on the host with hot reload
task invite    # prints the link to share
```

Open the printed link. You'll be asked for a display name once, then you're in the room.
Hit **Step up to DJ**, search your library, and queue something.

For anything past your own machine — a server your friends can actually reach — see
[Deploying](#deploying).

## Inviting people

`task invite` prints one link that everyone uses:

```
http://your.server:8080/?invite=<token>
```

The token is stored in the browser and stripped from the URL, so a reload keeps you
signed in as the same person — including your spot in the DJ rotation.

There's one shared secret rather than per-person invites, so revoking access for one
person means rotating `INVITE_TOKEN` and re-sharing the link. That's the tradeoff for
"no accounts, just a link."

## Commands

```bash
task dev         # app on host with hot reload, icecast/liquidsoap in docker
task invite      # print the invite link
task deploy      # build + start the full stack on a remote server, see below
task test        # unit tests (~6s, no services needed)
task test:e2e    # end-to-end against a running stack
task --list      # the rest
```

## How it works

```
Navidrome ──audio──> Liquidsoap ──MP3──> Icecast ──stream──> everyone's browser
                          ▲
                          │ "play this next" (telnet)
                          │
                    FastAPI app <──WebSocket──> browsers (queue, chat, votes)
```

The app never touches audio. It decides *what* should play and tells Liquidsoap to fetch
it straight from Navidrome; Liquidsoap encodes one continuous stream into Icecast. That's
why playback survives an app restart, and why everyone is in sync for free.

Full diagrams and the reasoning behind the fiddly parts are in
[`docs/architecture.md`](docs/architecture.md).

## Deploying

`task dev` is for your own machine only — the app isn't exposed anywhere. To put this in
front of friends, you need a server; `docker-compose.deploy.yml` assumes
[Traefik](https://traefik.io) discovering services via Docker labels for a friendly local
hostname (this was built against
[this Traefik setup](https://github.com/tsdaemon/theseus/tree/main/roles/services/traefik),
adjust the labels if yours differs). **Public HTTPS exposure is not Traefik's job here** —
it assumes something in front (a Cloudflare Tunnel, a proxy, whatever you already use)
terminates TLS and forwards to the NAS; Traefik only routes it once it's on the LAN.

```bash
cp .env.deploy.example .env.deploy
```

Edit `.env.deploy` the same way as `.env` (see [Setup](#setup)), plus:

- `APP_HOSTNAME` — the LAN-local hostname Traefik should route to the app (plain HTTP).
- `APP_PUBLIC_URL` — the URL friends actually use, from whatever fronts it publicly. Not
  necessarily the same host as `APP_HOSTNAME` — only used to print invite links.

Then, with a [Docker context](https://docs.docker.com/engine/context/working-with-contexts/)
named `theseus` pointed at the server over SSH:

```bash
task deploy        # builds on the remote daemon and starts icecast + liquidsoap + app
task deploy:logs   # tail it
task deploy:down   # stop it
```

`task deploy` runs `docker compose` against both `docker-compose.yml` (icecast +
liquidsoap, same as local) and `docker-compose.deploy.yml` (adds the `app` service, its
Traefik router labels, and a [Homepage](https://gethomepage.dev) dashboard widget showing
the live listener count via `GET /api/stats`). `docker compose up` recreates only the
services whose config or image changed, so a routine app-only deploy leaves liquidsoap
and icecast — and the live stream — running. Icecast's audio port is still published
directly rather than routed through Traefik — see `docs/architecture.md`.

## Development

The app is a [uv](https://docs.astral.sh/uv/) project (`app/`, src layout at
`app/src/tsdfm/`). The frontend is deliberately plain HTML/CSS/JS with no build step.

```bash
task dev     # uvicorn --reload on the host, against dockerized icecast/liquidsoap
task test    # unit tests
```

Unit tests fake Liquidsoap and Navidrome and compress the playback timings, so logic that
takes minutes in reality runs in milliseconds. The e2e suite drives the real WebSocket API
and cross-checks Icecast's live metadata — it queues real tracks, so don't run it while
people are listening.

If you're working on this with an AI coding agent, [`AGENTS.md`](AGENTS.md) has the
gotchas worth knowing before touching the playback path.

## Limitations

- **One room.** No multi-room support, by design.
- **State is a single JSON file, not a database.** Queues, rotation and chat are mirrored
  to disk and restored on restart, and the app re-attaches to whatever Liquidsoap is still
  broadcasting. Fine for one instance; two app processes sharing one state file would
  clobber each other.
- **Anyone with the link is in.** The invite token is the only access control. The app
  itself speaks plain HTTP with no TLS of its own — `task deploy` only routes it locally
  on the LAN; whatever exposes it publicly (see [Deploying](#deploying)) must terminate
  HTTPS in front of it.
- **Bandwidth is on you.** `STREAM_BITRATE` defaults to 320 kbps — about 144 MB/hour per
  listener, so a dozen friends need roughly 4 Mbps of upload. Drop it to 192 (~86 MB/hour,
  ~2 Mbps) if that's tight. Fine for a home connection, not a public station.

## A note on what you're broadcasting

This streams music from your own library to people you invite. That's the same shape as a
hobbyist internet radio station or a DJ set for friends — and, like those, public
performance licensing exists in the background even when the audience is small and
private. Worth knowing it's a real consideration if you ever point this at a wider
audience than a few friends.
