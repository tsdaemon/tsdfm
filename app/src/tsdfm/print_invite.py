import os
import secrets
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Overridable by task so one script serves both `task invite` (.env,
# INVITE_TOKEN) and `task deploy:invite` (.env.deploy, DEPLOY_INVITE_TOKEN) -
# see Taskfile.yml.
TOKEN_VAR = os.environ.get("TSDFM_INVITE_TOKEN_VAR", "INVITE_TOKEN")
RESTART_HINT = os.environ.get("TSDFM_RESTART_HINT", "restart the app")


def _env_path() -> Path:
    # Set by `task deploy:invite` so a freshly generated token is saved to
    # .env.deploy, not to whichever .env find_dotenv() happens to locate.
    override = os.environ.get("TSDFM_ENV_PATH")
    if override:
        return Path(override)
    found = find_dotenv(usecwd=True)
    if found:
        return Path(found)
    # No .env anywhere up the tree - create one at the repo root (two levels above
    # the installed package: src/tsdfm/ -> src/ -> app/ -> repo root).
    return Path(__file__).resolve().parents[3] / ".env"


def get_or_create_invite_token(env_path: Path) -> tuple[str, bool]:
    token = os.environ.get(TOKEN_VAR)
    if token:
        return token, False
    token = secrets.token_urlsafe(16)
    with env_path.open("a") as f:
        f.write(f"\n{TOKEN_VAR}={token}\n")
    return token, True


def main():
    env_path = _env_path()
    load_dotenv(env_path)
    token, generated = get_or_create_invite_token(env_path)
    base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")

    # Status/warnings go to stderr so stdout is just the link, safe to pipe/copy.
    if generated:
        print(
            f"*** Generated a new {TOKEN_VAR} and saved it to {env_path} ***\n"
            f"*** It has no effect until you {RESTART_HINT} ***",
            file=sys.stderr,
        )

    if base:
        print(f"{base}/?invite={token}")
    else:
        print(f"No APP_PUBLIC_URL set - share as: <your-app-url>/?invite={token}", file=sys.stderr)
        print(token)


if __name__ == "__main__":
    main()
