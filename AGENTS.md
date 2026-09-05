# AGENTS.md

Self-hosted, turntable.fm-style radio for a handful of friends. DJs take turns queueing
tracks from a private Navidrome library; everyone hears one live Icecast broadcast.

Read `docs/architecture.md` first — it has the diagrams and the reasoning behind the
non-obvious parts.

## Commands

Everything goes through [go-task](https://taskfile.dev):

```bash
task test          # unit tests, ~6s, no services needed
task test:e2e      # e2e against a running stack — DISRUPTIVE, see below
task dev           # app on host w/ hot reload + icecast/liquidsoap in docker
task deploy        # build + start the full stack on the remote NAS, behind Traefik
task invite        # print the shared invite link
task --list        # everything else
```

`docker-compose.yml` (icecast + liquidsoap only) is used both by `task dev` locally and as
the base for `task deploy`, which layers `docker-compose.deploy.yml` on top to add the
`app` service, Traefik routing labels, and a Homepage widget label. The app never runs in
local Docker — only via `task dev` (host) or `task deploy` (remote). See
`docs/architecture.md` for the full topology.

Python is managed with **uv** (`app/pyproject.toml`, src layout at `app/src/tsdfm/`).
Use `uv run ...`, not a hand-rolled venv. `uv.lock` is committed; `.venv` is not.

## The one fact that explains the system

**Audio never passes through the app.** The app is a control plane: it tells Liquidsoap
what to play over a telnet socket. Bytes go Navidrome → Liquidsoap → Icecast → browsers.

Two consequences that come up constantly:

- **Restarting the app does not interrupt playback.** Liquidsoap and Icecast are separate
  containers and keep broadcasting. If audio stopped, the app is not your suspect.
- **Restarting the app no longer wipes the room.** Queues, rotation and chat are mirrored
  to a JSON file after every mutation, and `Room.resume()` re-attaches to whatever
  Liquidsoap is still broadcasting by asking about the saved rid. Two app processes
  sharing one `STATE_PATH` *will* clobber each other, so dev and Docker keep separate
  paths on purpose.

## Load-bearing decisions — do not "simplify" these

Each of these looks like it could be simpler. Each was a bug that took real debugging.

1. **The app observes playback state; it never infers it.** `now_playing` is written
   *only* from what Liquidsoap reports in `_sync_once()`. `_start_next()` hands over a
   track and deliberately touches nothing else. Do not "helpfully" set `now_playing` when
   pushing — every desync in this project came from exactly that.
2. **Three reads, no guessing.** A pushed request is identified by an `annotate:`
   tag (`tsdfm_id`). It is *cueing* while its rid is in `queue.queue`, **on air** once it
   isn't, and *finished* when `request.metadata <rid>` comes back empty. Don't replace
   this with a timer, and don't try to infer it from `remaining()` alone — that reports
   whatever the output is playing, never *which request* it is.
3. **Never detect track-end from Icecast's title.** It only changes when the app pushes
   something new, so "wait for the title to change before pushing" deadlocks. Icecast
   metadata *is* good as read-only ground truth for "what is genuinely on air" — the e2e
   suite uses it that way.
4. **`LiquidsoapUnavailable` is not "nothing is there".** Observing calls raise it when
   the control channel fails; the sync loop then holds its last known state. Returning an
   empty answer instead makes a dropped socket read as "the track ended".
5. **`remove_user` deliberately leaves `dj_order` / `dj_queues` intact.** Identity is
   stable across reloads, so a dropped connection must not cost someone their DJ slot or
   queue. Only an explicit `step_down` clears them.
6. **The countdown comes from Liquidsoap, never from `track.duration`.** Navidrome's
   duration is the progress bar's *total* only. If it's wrong the bar is wrong, but
   playback stays correct — that separation is the whole point.
7. **`blank(duration=5.)` in `radio.liq` is not filler-for-fun.** It keeps the Icecast
   source connected during dead air. Remove it and the mount drops, disconnecting every
   listener between tracks.
8. **The frontend is deliberately vanilla JS with no build step**, but it is *not*
   ad-hoc: strictly `server message → setState → render(state) → DOM`. Renderers read only
   `state` — never the DOM, never a stashed previous value — and actions never touch the
   DOM. Vite/React were considered and rejected. Don't add a toolchain, and don't
   reintroduce cross-talking globals.
9. **Metadata parsing tolerates CRLF.** Liquidsoap's telnet ends lines with `\r\n`; a
   regex anchored to `"$` silently matches nothing against the real server while passing
   a naive test.

## Liquidsoap telnet

Port 1234, and **unauthenticated** — anyone who reaches it controls the radio.

The isolation comes from the compose port mapping, not from Liquidsoap's config:
`radio.liq` binds `0.0.0.0` *inside the container* (it must — the dockerized app reaches
it by service name over the compose network), while docker-compose publishes it as
`127.0.0.1:1234:1234` so only host processes can reach it. Don't "harden" the `bind_addr`
in `radio.liq`; that breaks `task dev`. Guard the compose mapping instead.

- Commands used: `queue.push annotate:tsdfm_id="...":<uri>`, `queue.queue`,
  `queue.remove <rid>`, `queue.skip`, `request.metadata <rid>`,
  `output.icecast.remaining`
- Replies are terminated by a line containing `END\r\n`
- **One TCP connection per command.** A shared socket interleaves replies and you read
  the previous command's tail.
- Run `help` over the socket to list commands for the installed version rather than
  guessing. Liquidsoap's API shifts between versions — `getenv` vs `environment.get`, and
  Icecast's `<changeowner>` nesting, both cost a debug cycle here.

## Environment gotchas on this machine

- **`PYTHONPATH` points at ROS.** Left set, pytest autoloads ROS plugins and dies with
  `ModuleNotFoundError: No module named 'lark'`. The task files clear it; if you invoke
  pytest directly, prefix `PYTHONPATH=`.
- **Check `docker context ls` before debugging containers.** It has been pointed at a
  remote server (`theseus`, `ssh://...`), so builds/runs land there, not locally.
  `docker context use default` for local.
- **`~/.docker/config.json` may carry a Docker Desktop `credsStore`** (`desktop.exe`),
  which breaks builds in WSL with `docker-credential-desktop.exe: executable file not
  found`. Removing the key is the fix.
- **Curling a container's bridge IP from the host doesn't work here.** Test from inside
  the compose network instead:
  `docker run --rm --network tsdfm_default curlimages/curl:latest -s http://icecast:6491/status-json.xsl`
  (icecast's internal port; `ICECAST_PORT` in `.env`, defaults to 6491)

## Testing

- **Unit** (`app/tests/unit/`) — `helpers.FakeLiquidsoap` models the *observable contract*
  (cueing → on air → gone) rather than the wire protocol, and `conftest` shrinks
  `SYNC_INTERVAL`, so behaviour that takes minutes in reality is verified in
  milliseconds. Drive transitions with `finish_track()` and `await room._sync_once()`.
  Add regression tests here, not to the e2e suite.
- **E2E** (`app/tests/e2e/`) — drives the real WebSocket API and cross-checks Icecast's
  live metadata. It **steps up as a DJ and queues real tracks**, so it changes what is
  playing. Never run it during a listening session; use a dev stack.
- E2E skips itself if the app or `INVITE_TOKEN` isn't available, so a bare `task test:all`
  degrades gracefully.

## Secrets

- `.env` is gitignored and holds real credentials (Navidrome password, Icecast passwords,
  `INVITE_TOKEN`). Never commit it, and don't echo those values into terminal output or
  test names.
- `INVITE_TOKEN` is the *only* access control — one shared secret behind the `?invite=`
  link. Rotating it locks everyone out, which is the accepted tradeoff for a single link.
- `client_id` (browser-generated, in localStorage) is an identity, not a credential — it
  is broadcast to other clients and that's fine.

## Conventions

- Ask before restarting a running stack or the dev server; it costs the live queue.
- Verify against the real services when touching the Liquidsoap/Icecast path — this
  stack's failures are almost always integration-shaped, not logic-shaped.
- Config is env vars only (`.env`, see `.env.example`). No config files, no CLI flags.
