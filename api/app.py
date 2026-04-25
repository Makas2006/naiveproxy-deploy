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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
USERS_FILE = STATE_DIR / "users.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
USERS_CADDY = Path(os.getenv("USERS_CADDY", "/etc/caddy/users.caddy"))
SITE_CADDY = Path(os.getenv("SITE_CADDY", "/etc/caddy/site.caddy"))
CADDYFILE = Path(os.getenv("CADDYFILE", "/etc/caddy/Caddyfile"))
CADDY_ADMIN_URL = os.getenv("CADDY_ADMIN_URL", "http://caddy:2019")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_"

app = FastAPI(title="NaiveProxy control API", docs_url=None, redoc_url=None)


class Settings(BaseModel):
    public_domain: str = ""
    secret_domain: str = ""
    acme_email: str = ""
    api_token: str = ""
    bot_token: str = ""
    admin_chat_id: str = ""


class SettingsUpdate(BaseModel):
    public_domain: str
    secret_domain: str
    acme_email: str
    api_token: str
    bot_token: str
    admin_chat_id: str


class UserCreateResponse(BaseModel):
    name: str
    password: str
    url: str


class UserListResponse(BaseModel):
    users: list[str]


def bootstrap_settings() -> Settings:
    return Settings(
        public_domain=os.getenv("PUBLIC_DOMAIN", ""),
        secret_domain=os.getenv("SECRET_DOMAIN", ""),
        acme_email=os.getenv("ACME_EMAIL", ""),
        api_token=os.getenv("API_TOKEN", ""),
        bot_token=os.getenv("BOT_TOKEN", ""),
        admin_chat_id=os.getenv("ADMIN_CHAT_ID", ""),
    )


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_settings() -> Settings:
    if not SETTINGS_FILE.exists():
        settings = bootstrap_settings()
        save_settings(settings)
        return settings
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return Settings(**data)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"invalid settings database: {exc}") from exc


def save_settings(settings: Settings) -> None:
    atomic_write(SETTINGS_FILE, settings.model_dump_json(indent=2) + "\n")


def require_token(x_api_token: Annotated[str | None, Header()] = None) -> None:
    api_token = load_settings().api_token
    if not api_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="api_token is not configured",
        )
    if not x_api_token or not secrets.compare_digest(x_api_token, api_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def load_users() -> dict[str, str]:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"invalid users database: {exc}") from exc


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


def render_site_caddy(settings: Settings) -> str:
    domain_line = ":443"
    domains = [settings.public_domain.strip(), settings.secret_domain.strip()]
    domains = [domain for domain in domains if domain]
    if domains:
        domain_line += ", " + ", ".join(domains)

    tls_line = ""
    if settings.acme_email.strip():
        tls_line = f"    tls {settings.acme_email.strip()}\n\n"

    return f"""{domain_line} {{
{tls_line}    encode zstd gzip

    forward_proxy {{
        import /etc/caddy/users.caddy
        hide_ip
        hide_via
        probe_resistance
    }}

    root * /srv/www
    file_server
}}
"""


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


async def persist_and_reload(users: dict[str, str], settings: Settings | None = None) -> None:
    atomic_write(USERS_CADDY, render_users_caddy(users))
    atomic_write(SITE_CADDY, render_site_caddy(settings or load_settings()))
    save_users(users)
    await reload_caddy()


def generate_password(length: int = 24) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


@app.get("/", response_class=HTMLResponse)
async def panel() -> str:
    return HTML_PAGE


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/settings", response_model=Settings, dependencies=[Depends(require_token)])
async def get_settings() -> Settings:
    return load_settings()


@app.put("/settings", dependencies=[Depends(require_token)])
async def update_settings(update: SettingsUpdate) -> dict[str, str]:
    old = load_settings()
    settings = Settings(**update.model_dump())
    save_settings(settings)
    atomic_write(SITE_CADDY, render_site_caddy(settings))
    await reload_caddy()

    restart_note = ""
    if settings.bot_token != old.bot_token:
        restart_note = " Restart bot container to apply bot_token."
    return {"status": "saved", "note": f"Caddy reloaded.{restart_note}"}


@app.get("/users", response_model=UserListResponse, dependencies=[Depends(require_token)])
async def list_users() -> UserListResponse:
    return UserListResponse(users=sorted(load_users()))


@app.post("/users/{name}", response_model=UserCreateResponse, dependencies=[Depends(require_token)])
async def add_user(name: str) -> UserCreateResponse:
    if not USERNAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="name must match [A-Za-z0-9_.-], max 32 chars")

    settings = load_settings()
    if not settings.public_domain:
        raise HTTPException(status_code=500, detail="public_domain is not configured")

    users = load_users()
    if name in users:
        raise HTTPException(status_code=409, detail="user already exists")

    password = generate_password()
    users[name] = password
    await persist_and_reload(users, settings)

    return UserCreateResponse(name=name, password=password, url=f"https://{name}:{password}@{settings.public_domain}")


@app.delete("/users/{name}", dependencies=[Depends(require_token)])
async def delete_user(name: str) -> dict[str, str]:
    users = load_users()
    if name not in users:
        raise HTTPException(status_code=404, detail="user not found")
    del users[name]
    await persist_and_reload(users)
    return {"status": "deleted", "name": name}


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NaiveProxy Admin</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #18212f; }
    main { max-width: 980px; margin: 0 auto; padding: 32px 20px; }
    h1 { font-size: 28px; margin: 0 0 20px; }
    section { background: #fff; border: 1px solid #dbe2ee; border-radius: 8px; padding: 18px; margin: 16px 0; }
    h2 { font-size: 18px; margin: 0 0 14px; }
    label { display: grid; gap: 6px; margin: 10px 0; font-size: 13px; color: #445167; }
    input { box-sizing: border-box; width: 100%; padding: 10px 12px; border: 1px solid #cbd5e1; border-radius: 6px; font: inherit; }
    button { border: 0; border-radius: 6px; padding: 10px 14px; font: inherit; cursor: pointer; background: #155eef; color: #fff; }
    button.secondary { background: #334155; }
    button.danger { background: #c2410c; }
    .row { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; }
    .row label { flex: 1 1 240px; }
    .status { margin-top: 10px; color: #445167; white-space: pre-wrap; }
    .users { display: grid; gap: 8px; }
    .user { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 10px 0; border-top: 1px solid #e2e8f0; }
    code { display: block; overflow-wrap: anywhere; background: #eef2ff; color: #1e1b4b; padding: 10px; border-radius: 6px; }
    @media (prefers-color-scheme: dark) {
      body { background: #111827; color: #e5e7eb; }
      section { background: #1f2937; border-color: #374151; }
      label, .status { color: #cbd5e1; }
      input { background: #111827; border-color: #4b5563; color: #e5e7eb; }
      .user { border-color: #374151; }
      code { background: #111827; color: #dbeafe; }
    }
  </style>
</head>
<body>
<main>
  <h1>NaiveProxy Admin</h1>

  <section>
    <h2>Login</h2>
    <div class="row">
      <label>API token <input id="token" type="password" autocomplete="current-password"></label>
      <button onclick="saveToken()">Save token</button>
      <button class="secondary" onclick="loadAll()">Refresh</button>
    </div>
    <div id="status" class="status"></div>
  </section>

  <section>
    <h2>Settings</h2>
    <label>Public domain <input id="public_domain"></label>
    <label>Secret probe domain <input id="secret_domain"></label>
    <label>ACME email <input id="acme_email"></label>
    <label>API token <input id="api_token"></label>
    <label>Telegram bot token <input id="bot_token"></label>
    <label>Admin chat ID <input id="admin_chat_id"></label>
    <button onclick="saveSettings()">Save settings</button>
  </section>

  <section>
    <h2>Users</h2>
    <div class="row">
      <label>New username <input id="new_user" placeholder="alice"></label>
      <button onclick="addUser()">Add</button>
    </div>
    <div id="created" class="status"></div>
    <div id="users" class="users"></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const fields = ["public_domain", "secret_domain", "acme_email", "api_token", "bot_token", "admin_chat_id"];

function saveToken() {
  localStorage.setItem("np_token", $("token").value);
  loadAll();
}

function token() {
  return localStorage.getItem("np_token") || $("token").value;
}

async function request(path, options = {}) {
  const headers = Object.assign({"X-API-Token": token()}, options.headers || {});
  const res = await fetch(path, Object.assign({}, options, {headers}));
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status}: ${text}`);
  return text ? JSON.parse(text) : {};
}

async function loadAll() {
  $("status").textContent = "Loading...";
  try {
    $("token").value = token();
    const settings = await request("/settings");
    fields.forEach((field) => $(field).value = settings[field] || "");
    await loadUsers();
    $("status").textContent = "Ready";
  } catch (err) {
    $("status").textContent = `Error: ${err.message}`;
  }
}

async function saveSettings() {
  const body = {};
  fields.forEach((field) => body[field] = $(field).value.trim());
  try {
    const result = await request("/settings", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    localStorage.setItem("np_token", body.api_token);
    $("token").value = body.api_token;
    $("status").textContent = result.note || "Saved";
  } catch (err) {
    $("status").textContent = `Save failed: ${err.message}`;
  }
}

async function loadUsers() {
  const result = await request("/users");
  $("users").innerHTML = "";
  result.users.forEach((name) => {
    const row = document.createElement("div");
    row.className = "user";
    row.innerHTML = `<span>${name}</span><button class="danger">Kick</button>`;
    row.querySelector("button").onclick = () => kickUser(name);
    $("users").appendChild(row);
  });
}

async function addUser() {
  const name = $("new_user").value.trim();
  if (!name) return;
  try {
    const result = await request(`/users/${encodeURIComponent(name)}`, {method: "POST"});
    $("created").innerHTML = `Created:<code>${result.url}</code>`;
    $("new_user").value = "";
    await loadUsers();
  } catch (err) {
    $("created").textContent = `Add failed: ${err.message}`;
  }
}

async function kickUser(name) {
  try {
    await request(`/users/${encodeURIComponent(name)}`, {method: "DELETE"});
    await loadUsers();
  } catch (err) {
    $("status").textContent = `Kick failed: ${err.message}`;
  }
}

window.addEventListener("load", () => {
  $("token").value = token();
});
</script>
</body>
</html>"""
