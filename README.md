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
- **Search your own library** — Navidrome/Subsonic, including cover art
- **Invite by link** — one shared link, no accounts, no signup
- **A log panel in the UI** so you can see what's failing without SSHing anywhere

## Requirements

- Docker + Docker Compose
- [go-task](https://taskfile.dev) for the commands below
- An existing **Navidrome** instance with your music (this stack does not run or manage it)
- [uv](https://docs.astral.sh/uv/) if you want to run the app outside Docker for development

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

Then:

```bash
task up        # build and start icecast + liquidsoap + app
task invite    # prints the link to share
```

Open the printed link. You'll be asked for a display name once, then you're in the room.
Hit **Step up to DJ**, search your library, and queue something.

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
task up          # full stack in docker
task down        # stop it
task restart     # rebuild + restart just the app
task logs        # tail everything
task invite      # print the invite link

task dev         # app on host with hot reload, icecast/liquidsoap in docker
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
- **State is in-memory.** Restarting the app clears queues and the DJ rotation. The audio
  keeps playing (Liquidsoap and Icecast are untouched), but the room resets — worth knowing
  before you redeploy mid-session.
- **Anyone with the link is in.** The invite token is the only access control, and the app
  speaks plain HTTP — put it behind a reverse proxy with TLS if it's exposed beyond a LAN
  or tunnel.
- **Bandwidth is on you.** Roughly 192 kbps per listener (~86 MB/hour each), so a dozen
  friends need about 2 Mbps of upload. Fine for a home connection, not a public station.

## A note on what you're broadcasting

This streams music from your own library to people you invite. That's the same shape as a
hobbyist internet radio station or a DJ set for friends — and, like those, public
performance licensing exists in the background even when the audience is small and
private. Worth knowing it's a real consideration if you ever point this at a wider
audience than a few friends.
