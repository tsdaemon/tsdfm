import os
import secrets
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _env_path() -> Path:
    found = find_dotenv(usecwd=True)
    if found:
        return Path(found)
    # No .env anywhere up the tree - create one at the repo root (two levels above
    # the installed package: src/tsdfm/ -> src/ -> app/ -> repo root).
    return Path(__file__).resolve().parents[3] / ".env"


def get_or_create_invite_token(env_path: Path) -> str:
    token = os.environ.get("INVITE_TOKEN")
    if token:
        return token
    token = secrets.token_urlsafe(16)
    with env_path.open("a") as f:
        f.write(f"\nINVITE_TOKEN={token}\n")
    print(f"(generated a new INVITE_TOKEN and saved it to {env_path} - restart the app to pick it up)")
    return token


def main():
    env_path = _env_path()
    load_dotenv(env_path)
    token = get_or_create_invite_token(env_path)
    base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    if base:
        print(f"{base}/?invite={token}")
    else:
        print(f"Invite token: {token}")
        print(f"Share as: <your-app-url>/?invite={token}")


if __name__ == "__main__":
    main()
