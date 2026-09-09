"""DJ breaks: a short spoken segment between songs, like a radio host cracking a joke
or dropping a fun fact about the track that just played.

Two hops, both best-effort and both off the room's sync loop:

1. an LLM (via OpenRouter, so several models can be compared) writes a line or two;
2. Piper (a local, self-hosted container - no API key, no per-use cost) voices it.

The result is a small WAV on a volume the liquidsoap container also mounts, so the room
plays it by local path through the exact same `queue.push` path as a real track. Any
failure here returns None and the room simply moves straight to the next song.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

import httpx

logger = logging.getLogger("dj_break")

DEFAULT_PROMPT = (
    "You are the host of a small internet radio station shared by a group of friends. "
    "Between songs you say one or two short spoken sentences: either a genuine fun fact "
    "about the track that just played (or its artist), or a light joke about it. "
    "Keep it under {max_words} words. Sound like talk radio, not writing: no emojis, no "
    "markdown, no stage directions, no 'and now' segues, and never read out a track list. "
    "You may also be told who just joined or left the room and the last few chat "
    "messages - you can nod to those (welcome someone by name, riff on the room's mood) "
    "but keep it brief, keep the music first, and don't repeat chat messages back word "
    "for word."
)


@dataclass
class BreakClip:
    # `path` is how the liquidsoap container sees the file (its /clips mount); `local_path`
    # is where this process actually wrote it, which differs under `task dev` where the app
    # runs on the host. `model` is what OpenRouter reports it actually used, so breaks from
    # different models can be told apart in the logs and the room.
    path: str
    local_path: str
    text: str
    model: str


class DjBreakStudio:
    def __init__(
        self,
        *,
        openrouter_url: str,
        api_key: Optional[str],
        models: list[str],
        piper_url: str,
        cache_dir: Path,
        clips_dir: str,
        max_words: int = 40,
        every_n: int = 1,
        prompt_file: Optional[Path] = None,
    ):
        self.openrouter_url = openrouter_url.rstrip("/")
        self.api_key = api_key or ""
        self.models = [m.strip() for m in models if m.strip()] or ["openai/gpt-4o-mini"]
        self.piper_url = piper_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.clips_dir = clips_dir.rstrip("/")
        self.max_words = max_words
        self.every_n = max(1, every_n)
        self.prompt_file = Path(prompt_file) if prompt_file else None
        self._model_idx = 0

    @property
    def enabled(self) -> bool:
        """Without a key there is nothing to write the script; the UI toggle still works,
        breaks just never get generated."""
        return bool(self.api_key)

    def _system_prompt(self) -> str:
        """Re-read on every break so the wording can be tuned while the room runs. The
        file is gitignored on purpose - it's where inside jokes and friends' names go."""
        if self.prompt_file:
            try:
                raw = self.prompt_file.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            # Lines starting with '#' are notes to whoever edits the file, not prompt.
            text = "\n".join(
                ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")
            ).strip()
            if text:
                return text.replace("{max_words}", str(self.max_words))
        return DEFAULT_PROMPT.format(max_words=self.max_words)

    def _next_model(self) -> str:
        model = self.models[self._model_idx % len(self.models)]
        self._model_idx += 1
        return model

    async def script_and_voice(
        self,
        just_played: dict,
        coming_up: Optional[dict],
        context: Optional[dict] = None,
    ) -> Optional[BreakClip]:
        if not self.enabled:
            return None
        model = self._next_model()
        text = await self._script(model, just_played, coming_up, context or {})
        if not text:
            return None
        audio = await self._voice(text)
        if not audio:
            return None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            name = f"{uuid4().hex}.wav"
            local = self.cache_dir / name
            local.write_bytes(audio)
        except OSError as exc:
            logger.warning("Could not write DJ break clip: %s", exc)
            return None
        return BreakClip(
            path=f"{self.clips_dir}/{name}",
            local_path=str(local),
            text=text,
            model=model,
        )

    async def _script(
        self,
        model: str,
        just_played: dict,
        coming_up: Optional[dict],
        context: dict,
    ) -> Optional[str]:
        lines = [
            f"Just played: {just_played.get('title', '?')} by "
            f"{just_played.get('artist', '?')}."
        ]
        if coming_up:
            lines.append(
                f"Coming up next: {coming_up.get('title', '?')} by "
                f"{coming_up.get('artist', '?')}."
            )
        events = [e for e in (context.get("events") or []) if e]
        if events:
            lines.append("In the room just now: " + "; ".join(events) + ".")
        chat = [m for m in (context.get("chat") or []) if m.get("text")]
        if chat:
            lines.append("Recent chat:")
            lines += [f"  {m.get('user', '?')}: {m['text']}" for m in chat]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{self.openrouter_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        # OpenRouter asks callers to identify themselves; harmless if ignored.
                        "HTTP-Referer": "https://github.com/tsdaemon/tsdfm",
                        "X-Title": "tsdfm",
                    },
                    json={
                        "model": model,
                        "max_tokens": 200,
                        "temperature": 0.9,
                        "messages": [
                            {"role": "system", "content": self._system_prompt()},
                            {"role": "user", "content": " ".join(lines)},
                        ],
                    },
                )
                resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
            logger.warning("DJ break script (%s) failed: %s", model, type(exc).__name__)
            return None
        return _tidy(raw, self.max_words)

    async def _voice(self, text: str) -> Optional[bytes]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.piper_url + "/",
                    content=text.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
                resp.raise_for_status()
            if not resp.content:
                raise ValueError("empty audio")
            return resp.content
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("DJ break voice (Piper) failed: %s", type(exc).__name__)
            return None


def _tidy(raw: str, max_words: int) -> str:
    """Strip wrapping quotes / whitespace an LLM sometimes adds, and hard-cap the length
    so a runaway response can't produce a two-minute 'break'."""
    text = " ".join((raw or "").split()).strip().strip('"').strip()
    # Drop a leading stage direction like "[laughs]" or "(chuckling)".
    text = re.sub(r"^[\[(][^\])]*[\])]\s*", "", text).strip()
    words = text.split()
    if len(words) > max_words * 2:
        text = " ".join(words[: max_words * 2]).rstrip(",;:") + "."
    return text
