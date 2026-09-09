"""DjBreakStudio in isolation: the OpenRouter -> Piper -> file wiring, with both HTTP
hops stubbed."""

import httpx
import respx

from tsdfm.dj_break import DjBreakStudio, _tidy

OR = "https://openrouter.test/api/v1"
PIPER = "http://piper.test:5000"


def _studio(tmp_path, **kw):
    return DjBreakStudio(
        openrouter_url=OR,
        api_key="sk-test",
        models=["vendor/model-1", "vendor/model-2"],
        piper_url=PIPER,
        cache_dir=tmp_path,
        clips_dir="/clips",
        **kw,
    )


async def test_disabled_without_a_key(tmp_path):
    studio = _studio(tmp_path)
    studio.api_key = ""
    assert studio.enabled is False
    assert await studio.script_and_voice({"title": "X", "artist": "Y"}, None) is None


@respx.mock
async def test_writes_a_clip_and_reports_the_model(tmp_path):
    respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "vendor/model-1",
                "choices": [{"message": {"content": '"That was a great one."'}}],
            },
        )
    )
    piper = respx.post(f"{PIPER}/").mock(
        return_value=httpx.Response(200, content=b"RIFFfakewav", headers={"content-type": "audio/wav"})
    )

    studio = _studio(tmp_path)
    clip = await studio.script_and_voice(
        {"title": "Song", "artist": "Band"}, {"title": "Next", "artist": "Other"}
    )

    assert clip is not None
    assert clip.model == "vendor/model-1"
    assert clip.text == "That was a great one."           # wrapping quotes stripped
    assert clip.path.startswith("/clips/") and clip.path.endswith(".wav")
    assert (tmp_path / clip.path.split("/")[-1]).read_bytes() == b"RIFFfakewav"
    # Piper was handed the spoken line, not JSON.
    assert piper.calls.last.request.content == b"That was a great one."


@respx.mock
async def test_models_rotate_per_call(tmp_path):
    respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"model": "echo", "choices": [{"message": {"content": "hi"}}]}
        )
    )
    respx.post(f"{PIPER}/").mock(return_value=httpx.Response(200, content=b"wav"))

    studio = _studio(tmp_path)
    await studio.script_and_voice({"title": "A", "artist": "x"}, None)
    await studio.script_and_voice({"title": "B", "artist": "x"}, None)

    sent = [
        r.request for r in respx.calls if r.request.url.path.endswith("chat/completions")
    ]
    import json

    assert json.loads(sent[0].content)["model"] == "vendor/model-1"
    assert json.loads(sent[1].content)["model"] == "vendor/model-2"


@respx.mock
async def test_chat_and_room_events_reach_the_prompt(tmp_path):
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": "line"}}]}
        )
    )
    respx.post(f"{PIPER}/").mock(return_value=httpx.Response(200, content=b"wav"))

    studio = _studio(tmp_path)
    await studio.script_and_voice(
        {"title": "Song", "artist": "Band"},
        None,
        {
            "events": ["Ann joined", "Bo stepped up to DJ"],
            "chat": [{"user": "Ann", "text": "turn it up"}],
        },
    )

    import json

    sent = json.loads(route.calls.last.request.content)
    user_msg = sent["messages"][-1]["content"]
    assert "Ann joined; Bo stepped up to DJ." in user_msg
    assert "Ann: turn it up" in user_msg


@respx.mock
async def test_piper_failure_returns_none_no_file(tmp_path):
    respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": "line"}}]}
        )
    )
    respx.post(f"{PIPER}/").mock(return_value=httpx.Response(503))

    studio = _studio(tmp_path)
    assert await studio.script_and_voice({"title": "A", "artist": "x"}, None) is None
    assert list(tmp_path.glob("*.wav")) == []


@respx.mock
async def test_openrouter_failure_returns_none(tmp_path):
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(500))
    studio = _studio(tmp_path)
    assert await studio.script_and_voice({"title": "A", "artist": "x"}, None) is None


def test_prompt_file_overrides_default_and_strips_comments(tmp_path):
    p = tmp_path / "prompt.txt"
    p.write_text("# a note for humans\nBe brief. Cap: {max_words} words.\n")
    studio = _studio(tmp_path, prompt_file=p, max_words=25)
    prompt = studio._system_prompt()
    assert prompt == "Be brief. Cap: 25 words."

    p.unlink()
    assert "radio station" in studio._system_prompt()  # falls back to the built-in


def test_tidy_caps_a_runaway_response():
    long = " ".join(["word"] * 200)
    assert len(_tidy(long, 40).split()) <= 81
    assert _tidy('  "[laughs] hello there"  ', 40) == "hello there"
