"""Minimal HTTP front for the Piper TTS binary.

POST /            body (or ?text=) is the line to speak -> audio/wav
GET  /health      readiness probe

Kept tiny on purpose: the app already treats a failed break as "just play the next
song", so this only needs to be a thin, honest shell around the binary.
"""

import os
import pathlib
import subprocess
import tempfile

from flask import Flask, Response, request

app = Flask(__name__)

PIPER_BIN = "/opt/piper/piper"
VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
MODEL = f"/voices/{VOICE}.onnx"
MAX_CHARS = 800


@app.get("/health")
def health() -> Response:
    return Response("ok\n", mimetype="text/plain")


@app.post("/")
def tts() -> Response:
    text = (request.get_data(as_text=True) or request.args.get("text", "")).strip()
    if not text:
        return Response("no text\n", status=400, mimetype="text/plain")
    text = text[:MAX_CHARS]

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        proc = subprocess.run(
            [PIPER_BIN, "--model", MODEL, "--output_file", tmp.name],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return Response(
                proc.stderr or b"piper failed\n", status=500, mimetype="text/plain"
            )
        data = pathlib.Path(tmp.name).read_bytes()
    except subprocess.TimeoutExpired:
        return Response("piper timed out\n", status=504, mimetype="text/plain")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    return Response(data, mimetype="audio/wav")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
