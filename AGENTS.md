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
task up            # whole stack in docker
task invite        # print the shared invite link
task --list        # everything else
```

Python is managed with **uv** (`app/pyproject.toml`, src layout at `app/src/tsdfm/`).
Use `uv run ...`, not a hand-rolled venv. `uv.lock` is committed; `.venv` is not.

## The one fact that explains the system

**Audio never passes through the app.** The app is a control plane: it tells Liquidsoap
what to play over a telnet socket. Bytes go Navidrome → Liquidsoap → Icecast → browsers.

Two consequences that come up constantly:

- **Restarting the app does not interrupt playback.** Liquidsoap and Icecast are separate
  containers and keep broadcasting. If audio stopped, the app is not your suspect.
- **Restarting the app *does* wipe the room** (queues, rotation, now-playing are in-memory).
  `task dev` hot-reloads on every edit to `app/src/`, so editing while someone is
  listening silently destroys their queue. Say so before doing it.

## Load-bearing decisions — do not "simplify" these

Each of these looks like it could be simpler. Each was a bug that took real debugging.

1. **Track-end detection polls Liquidsoap's `output.icecast.remaining`.** Do *not* replace
   it with `sleep(track.duration)`. Navidrome durations come from file tags and lie on
   mistagged files; a short lie made the UI say "Nothing playing" while audio kept going.
2. **The advance safety cap must not derive from the reported duration.** An earlier fix
   capped the wait at `duration * 3` — with a bogus 5s duration on a 201s track it still
   cut off at 65s. It's a flat `MAX_TRACK_SAFETY_SECONDS`, and its only job is surviving
   an unreachable Liquidsoap.
3. **Never detect track-end from Icecast's title.** It only changes when the app pushes
   something new, so "wait for the title to change before pushing" deadlocks. Icecast
   metadata *is* good as read-only ground truth for "what is genuinely on air" — the e2e
   suite uses it that way.
4. **A `None` from `remaining()` means "unknown", not "finished".** Treat it as still
   playing and retry, or a transient telnet blip cuts songs short.
5. **`remove_user` deliberately leaves `dj_order` / `dj_queues` intact.** Identity is
   stable across reloads, so a dropped connection must not cost someone their DJ slot or
   queue. Only an explicit `step_down` clears them.
6. **`_start_track` must not cancel the advance task when it is running inside it.**
   Chain-advance runs `_start_track` from within the outgoing track's task; cancelling
   unconditionally is self-cancellation.
7. **`blank(duration=5.)` in `radio.liq` is not filler-for-fun.** It keeps the Icecast
   source connected during dead air. Remove it and the mount drops, disconnecting every
   listener between tracks.
8. **The frontend is deliberately vanilla JS with no build step.** Vite/React were
   considered and rejected — it's ~400 lines of DOM code. Don't add a toolchain.

## Liquidsoap telnet

Port 1234, and **unauthenticated** — anyone who reaches it controls the radio.

The isolation comes from the compose port mapping, not from Liquidsoap's config:
`radio.liq` binds `0.0.0.0` *inside the container* (it must — the dockerized app reaches
it by service name over the compose network), while docker-compose publishes it as
`127.0.0.1:1234:1234` so only host processes can reach it. Don't "harden" the `bind_addr`
in `radio.liq`; that breaks `task up`. Guard the compose mapping instead.

- Commands used: `queue.push <uri>`, `queue.skip`, `output.icecast.remaining`
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
  `docker run --rm --network tsdfm_default curlimages/curl:latest -s http://icecast:8000/status-json.xsl`

## Testing

- **Unit** (`app/tests/unit/`) — fakes Liquidsoap/Navidrome and monkeypatches the advance
  loop timings, so logic that takes minutes in reality is verified in milliseconds.
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
