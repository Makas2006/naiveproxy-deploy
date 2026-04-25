import json
import os
import re
import secrets
import string
import tempfile
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
USERS_FILE = STATE_DIR / "users.json"
USERS_CADDY = Path(os.getenv("USERS_CADDY", "/etc/caddy/users.caddy"))
CADDYFILE = Path(os.getenv("CADDYFILE", "/etc/caddy/Caddyfile"))
CADDY_ADMIN_URL = os.getenv("CADDY_ADMIN_URL", "http://caddy:2019")
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "")
API_TOKEN = os.getenv("API_TOKEN", "")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_"

app = FastAPI(title="NaiveProxy control API", docs_url=None, redoc_url=None)


class UserCreateResponse(BaseModel):
    name: str
    password: str
    url: str


class UserListResponse(BaseModel):
    users: list[str]


def require_token(x_api_token: Annotated[str | None, Header()] = None) -> None:
    if not API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_TOKEN is not configured",
        )
    if not x_api_token or not secrets.compare_digest(x_api_token, API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def load_users() -> dict[str, str]:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"invalid users database: {exc}") from exc


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def save_users(users: dict[str, str]) -> None:
    serialized = json.dumps(users, indent=2, sort_keys=True) + "\n"
    atomic_write(USERS_FILE, serialized)


def render_users_caddy(users: dict[str, str]) -> str:
    lines = [
        "# Managed by the API container.",
        "# Do not edit by hand while the API is running.",
    ]
    for name in sorted(users):
        lines.append(f"basic_auth {name} {users[name]}")
    return "\n".join(lines) + "\n"


async def reload_caddy() -> None:
    caddyfile = CADDYFILE.read_text(encoding="utf-8")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{CADDY_ADMIN_URL}/load",
            content=caddyfile,
            headers={"Content-Type": "text/caddyfile"},
        )
    if response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"caddy reload failed: {response.status_code} {response.text}",
        )


async def persist_and_reload(users: dict[str, str]) -> None:
    atomic_write(USERS_CADDY, render_users_caddy(users))
    save_users(users)
    await reload_caddy()


def generate_password(length: int = 24) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users", response_model=UserListResponse, dependencies=[Depends(require_token)])
async def list_users() -> UserListResponse:
    return UserListResponse(users=sorted(load_users()))


@app.post("/users/{name}", response_model=UserCreateResponse, dependencies=[Depends(require_token)])
async def add_user(name: str) -> UserCreateResponse:
    if not USERNAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="name must match [A-Za-z0-9_.-], max 32 chars")
    if not PUBLIC_DOMAIN:
        raise HTTPException(status_code=500, detail="PUBLIC_DOMAIN is not configured")

    users = load_users()
    if name in users:
        raise HTTPException(status_code=409, detail="user already exists")

    password = generate_password()
    users[name] = password
    await persist_and_reload(users)

    return UserCreateResponse(name=name, password=password, url=f"https://{name}:{password}@{PUBLIC_DOMAIN}")


@app.delete("/users/{name}", dependencies=[Depends(require_token)])
async def delete_user(name: str) -> dict[str, str]:
    users = load_users()
    if name not in users:
        raise HTTPException(status_code=404, detail="user not found")
    del users[name]
    await persist_and_reload(users)
    return {"status": "deleted", "name": name}
