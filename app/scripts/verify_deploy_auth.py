"""Read-only production authentication check; never prints credentials or queues tracks."""
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx

root = Path(__file__).resolve().parents[2]
config = {**dotenv_values(root / ".env"), **dotenv_values(root / ".env.deploy")}
base = config["APP_PUBLIC_URL"].rstrip("/")
stream = config["ICECAST_STREAM_URL"]
assert urlsplit(base).scheme == "https"
assert urlsplit(stream).hostname == urlsplit(base).hostname, "App and stream must share a hostname"
with httpx.Client(timeout=20) as client:
    for path in ["/api/stats", "/api/search?q=", "/api/cover-art?id=test", "/api/logs"]:
        status = client.get(base + path).status_code
        print(f"Anonymous {path.split('?')[0]}: {status}")
        assert status == 401
    with client.stream("GET", stream) as response:
        print(f"Anonymous stream: {response.status_code}")
        assert response.status_code in (401, 403)
    response = client.post(base + "/api/session", json={"invite": config["DEPLOY_INVITE_TOKEN"]})
    assert response.status_code == 204, "Invite exchange failed"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert client.get(base + "/api/search?q=").status_code == 200
    with client.stream("GET", stream) as response:
        print(f"Authenticated stream: {response.status_code}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert next(response.iter_bytes())
    print("PASS: public APIs and stream require authentication; valid invite receives audio")
