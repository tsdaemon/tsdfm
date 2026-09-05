"""Isolated real Icecast/Liquidsoap test; never joins or changes the live room."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import httpx

root = Path(__file__).resolve().parents[3]
project = "tsdfm-auth-" + uuid.uuid4().hex[:8]
config = {"services": {
    "app": {
        "image": "tsdfm-app:latest", "ports": ["127.0.0.1::8080"],
        "volumes": [f"{root}/app/src:/app/src:ro"],
        "environment": {"PYTHONPATH": "/app/src", "NAVIDROME_URL": "http://unused", "NAVIDROME_USERNAME": "test", "NAVIDROME_PASSWORD": "test", "INVITE_TOKEN": "integration-invite", "ICECAST_STREAM_URL": "/radio.mp3", "LIQUIDSOAP_HOST": "liquidsoap", "STATE_PATH": "/tmp/state.json"},
    },
    "icecast": {
        "image": "tsdfm-icecast:latest", "entrypoint": ["sh", "/entrypoint.sh"], "ports": ["127.0.0.1::6491"],
        "volumes": [f"{root}/icecast/icecast.xml.template:/etc/icecast2/icecast.xml.template:ro", f"{root}/icecast/entrypoint.sh:/entrypoint.sh:ro"],
        "environment": {"ICECAST_SOURCE_PASSWORD": "source-test", "ICECAST_ADMIN_PASSWORD": "admin-test", "ICECAST_RELAY_PASSWORD": "relay-test", "ICECAST_HOSTNAME": "localhost", "ICECAST_PORT": "6491", "ICECAST_AUTH_URL": "http://app:8080/api/stream-auth"},
    },
    "liquidsoap": {
        "image": "tsdfm-liquidsoap:latest",
        "volumes": [f"{root}/liquidsoap/radio.liq:/radio.liq:ro"],
        "environment": {"ICECAST_SOURCE_PASSWORD": "source-test", "ICECAST_PORT": "6491", "STREAM_BITRATE": "128"},
        "depends_on": ["icecast"],
    },
}}
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "compose.json"
    path.write_text(json.dumps(config))
    command = ["docker", "--context", "default", "compose", "-p", project, "-f", str(path)]

    def docker(*args):
        return subprocess.check_output(command + list(args), text=True).strip()

    try:
        docker("up", "-d")
        app = "http://" + docker("port", "app", "8080")
        stream = "http://" + docker("port", "icecast", "6491") + "/radio.mp3"
        with httpx.Client(timeout=5) as client:
            for attempt in range(60):
                try:
                    if client.get(app + "/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("App did not start")
            for attempt in range(60):
                status = client.get(stream.rsplit("/", 1)[0] + "/status-json.xsl")
                if "source" in status.json().get("icestats", {}):
                    break
                time.sleep(1)
            else:
                raise AssertionError("Liquidsoap did not establish the test stream")
            for endpoint in ["/api/search?q=x", "/api/cover-art?id=x", "/api/logs"]:
                assert client.get(app + endpoint).status_code == 401
            with client.stream("GET", stream) as response:
                assert response.status_code in (401, 403), response.status_code
            assert client.post(app + "/api/session", json={"invite": "bad"}).status_code == 401
            assert client.post(app + "/api/session", json={"invite": "integration-invite"}).status_code == 204
            assert client.get(app + "/api/logs").status_code == 200
            with client.stream("GET", stream) as response:
                assert response.status_code == 200, response.status_code
                assert response.headers["content-type"] == "audio/mpeg"
                assert next(response.iter_bytes())
            client.cookies.clear()
            client.cookies.set("tsdfm_session", "forged")
            with client.stream("GET", stream) as response:
                assert response.status_code in (401, 403)
            client.cookies.clear()
            docker("stop", "app")
            with client.stream("GET", stream, timeout=30) as response:
                assert response.status_code in (401, 403)
        print("PASS: real Icecast denies anonymous/forged sessions and auth downtime; valid session receives audio; private APIs require auth")
    except Exception:
        print(docker("logs", "--tail", "30"))
        raise
    finally:
        docker("down", "--volumes")
