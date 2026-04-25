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
    admin_username: str = "admin"
    admin_password: str = "admin"


class SettingsUpdate(BaseModel):
    public_domain: str
    secret_domain: str
    acme_email: str
    api_token: str
    bot_token: str
    admin_chat_id: str
    admin_username: str
    admin_password: str


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
        api_token=os.getenv("API_TOKEN", "") or secrets.token_urlsafe(32),
        bot_token=os.getenv("BOT_TOKEN", ""),
        admin_chat_id=os.getenv("ADMIN_CHAT_ID", ""),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin"),
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


def require_auth(
    x_api_token: Annotated[str | None, Header()] = None,
    x_admin_username: Annotated[str | None, Header()] = None,
    x_admin_password: Annotated[str | None, Header()] = None,
) -> None:
    settings = load_settings()
    api_token = settings.api_token
    if api_token and x_api_token and secrets.compare_digest(x_api_token, api_token):
        return

    username_ok = bool(x_admin_username) and secrets.compare_digest(
        x_admin_username, settings.admin_username
    )
    password_ok = bool(x_admin_password) and secrets.compare_digest(
        x_admin_password, settings.admin_password
    )
    if username_ok and password_ok:
        return

    if not api_token and not settings.admin_username:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="admin credentials are not configured",
        )
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
    domains = [settings.public_domain.strip(), settings.secret_domain.strip()]
    domains = [domain for domain in domains if domain]
    domain_line = "http://:443"
    if domains:
        domain_line = ":443, " + ", ".join(domains)

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


@app.get("/settings", response_model=Settings, dependencies=[Depends(require_auth)])
async def get_settings() -> Settings:
    return load_settings()


@app.put("/settings", dependencies=[Depends(require_auth)])
async def update_settings(update: SettingsUpdate) -> dict[str, str]:
    old = load_settings()
    settings = Settings(**update.model_dump())
    old_site = SITE_CADDY.read_text(encoding="utf-8") if SITE_CADDY.exists() else ""
    atomic_write(SITE_CADDY, render_site_caddy(settings))
    try:
        await reload_caddy()
    except HTTPException:
        atomic_write(SITE_CADDY, old_site)
        try:
            await reload_caddy()
        finally:
            raise
    save_settings(settings)

    restart_note = ""
    if settings.bot_token != old.bot_token:
        restart_note = " Restart bot container to apply bot_token."
    return {"status": "saved", "note": f"Caddy reloaded.{restart_note}"}


@app.get("/users", response_model=UserListResponse, dependencies=[Depends(require_auth)])
async def list_users() -> UserListResponse:
    return UserListResponse(users=sorted(load_users()))


@app.post("/users/{name}", response_model=UserCreateResponse, dependencies=[Depends(require_auth)])
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


@app.delete("/users/{name}", dependencies=[Depends(require_auth)])
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
  <title>NaiveProxy Panel</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --bg: #0b1120;
      --panel: #111827;
      --panel-2: #0f172a;
      --line: #243044;
      --text: #e5edf8;
      --muted: #8ea0b8;
      --accent: #1677ff;
      --accent-2: #22c55e;
      --danger: #ef4444;
      --warn: #f59e0b;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); }
    button, input { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 244px 1fr; }
    .side { background: #070d19; border-right: 1px solid var(--line); padding: 20px 14px; display: flex; flex-direction: column; gap: 18px; }
    .brand { padding: 0 10px 14px; border-bottom: 1px solid var(--line); }
    .brand-title { font-size: 18px; font-weight: 700; letter-spacing: .2px; }
    .brand-sub { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .nav { display: grid; gap: 6px; }
    .nav button { width: 100%; border: 0; border-radius: 8px; padding: 11px 12px; color: var(--muted); background: transparent; text-align: left; cursor: pointer; transition: .16s ease; }
    .nav button:hover, .nav button.active { background: #13223a; color: #fff; }
    .side-foot { margin-top: auto; color: var(--muted); font-size: 12px; padding: 10px; border-top: 1px solid var(--line); }
    .main { min-width: 0; }
    .top { height: 66px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background: rgba(15, 23, 42, .82); position: sticky; top: 0; backdrop-filter: blur(12px); z-index: 4; }
    .title { font-size: 18px; font-weight: 700; }
    .status-pill { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 7px 11px; color: var(--muted); background: #0b1222; font-size: 13px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent-2); box-shadow: 0 0 14px var(--accent-2); }
    .content { padding: 24px; display: grid; gap: 18px; }
    .view { display: none; animation: rise .18s ease-out; }
    .view.active { display: grid; gap: 18px; }
    @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
    .metric, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .metric { padding: 16px; }
    .metric-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    .metric-value { margin-top: 10px; font-size: 26px; font-weight: 750; }
    .panel { overflow: hidden; }
    .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 15px 16px; border-bottom: 1px solid var(--line); }
    .panel-title { font-size: 15px; font-weight: 700; }
    .panel-body { padding: 16px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    label { display: grid; gap: 7px; color: var(--muted); font-size: 13px; }
    input { width: 100%; border: 1px solid var(--line); border-radius: 7px; padding: 10px 11px; background: #0a1220; color: var(--text); outline: none; transition: .16s ease; }
    input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(22, 119, 255, .15); }
    .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .btn { border: 0; border-radius: 7px; padding: 10px 13px; color: #fff; background: var(--accent); cursor: pointer; transition: .16s ease; }
    .btn:hover { filter: brightness(1.08); transform: translateY(-1px); }
    .btn.secondary { background: #263246; }
    .btn.danger { background: var(--danger); }
    .table { width: 100%; border-collapse: collapse; }
    .table th, .table td { padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; font-size: 13px; }
    .table th { color: var(--muted); font-weight: 600; background: #0d1526; }
    .empty { padding: 22px; color: var(--muted); text-align: center; }
    .code { display: block; word-break: break-all; border: 1px solid var(--line); border-radius: 7px; padding: 12px; background: #081120; color: #b8d7ff; }
    .toast { min-height: 22px; color: var(--muted); white-space: pre-wrap; font-size: 13px; }
    .login { max-width: 420px; margin: 12vh auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 22px; }
    .login h1 { margin: 0 0 4px; font-size: 24px; }
    .login p { margin: 0 0 18px; color: var(--muted); }
    .hidden { display: none !important; }
    @media (max-width: 860px) {
      .shell { grid-template-columns: 1fr; }
      .side { position: static; }
      .grid, .form-grid { grid-template-columns: 1fr; }
      .top { position: static; padding: 0 16px; }
      .content { padding: 16px; }
    }
  </style>
</head>
<body>
  <div id="login" class="login">
    <h1>NaiveProxy Panel</h1>
    <p>Default login is admin / admin.</p>
    <label>Username <input id="login_user" autocomplete="username" value="admin"></label>
    <label>Password <input id="login_pass" type="password" autocomplete="current-password" value="admin"></label>
    <div class="actions" style="margin-top:14px">
      <button class="btn" onclick="saveLogin()">Login</button>
    </div>
    <div id="login_status" class="toast" style="margin-top:12px"></div>
  </div>

  <div id="app" class="shell hidden">
    <aside class="side">
      <div class="brand">
        <div class="brand-title">NaiveProxy</div>
        <div class="brand-sub">Caddy forwardproxy control</div>
      </div>
      <nav class="nav">
        <button class="active" data-view="dashboard" onclick="showView('dashboard')">Dashboard</button>
        <button data-view="clients" onclick="showView('clients')">Clients</button>
        <button data-view="settings" onclick="showView('settings')">Settings</button>
      </nav>
      <div class="side-foot">Panel: http://SERVER_IP:3000<br>Proxy: domain:443</div>
    </aside>

    <main class="main">
      <header class="top">
        <div class="title" id="page_title">Dashboard</div>
        <div class="actions">
          <span class="status-pill"><span class="dot"></span><span id="top_status">Online</span></span>
          <button class="btn secondary" onclick="loadAll()">Refresh</button>
          <button class="btn secondary" onclick="logout()">Logout</button>
        </div>
      </header>

      <div class="content">
        <section id="dashboard" class="view active">
          <div class="grid">
            <div class="metric"><div class="metric-label">Active clients</div><div class="metric-value" id="metric_users">0</div></div>
            <div class="metric"><div class="metric-label">Public domain</div><div class="metric-value" id="metric_domain">-</div></div>
            <div class="metric"><div class="metric-label">HTTP stack</div><div class="metric-value">H2/H3</div></div>
            <div class="metric"><div class="metric-label">Probe resistance</div><div class="metric-value">On</div></div>
          </div>
          <div class="panel">
            <div class="panel-head"><div class="panel-title">Quick add client</div></div>
            <div class="panel-body">
              <div class="actions">
                <label style="flex:1 1 260px">Username <input id="new_user_dash" placeholder="client01"></label>
                <button class="btn" onclick="addUserFrom('new_user_dash')">Add client</button>
              </div>
              <div id="created" class="toast" style="margin-top:12px"></div>
            </div>
          </div>
        </section>

        <section id="clients" class="view">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Clients</div>
              <div class="actions">
                <input id="new_user" placeholder="new username" style="width:220px">
                <button class="btn" onclick="addUserFrom('new_user')">Add</button>
              </div>
            </div>
            <div id="users"></div>
          </div>
        </section>

        <section id="settings" class="view">
          <div class="panel">
            <div class="panel-head"><div class="panel-title">Proxy settings</div></div>
            <div class="panel-body form-grid">
              <label>Public domain <input id="public_domain" placeholder="proxy.example.com"></label>
              <label>Secret probe domain <input id="secret_domain" placeholder="secret.example.com"></label>
              <label>ACME email <input id="acme_email" placeholder="admin@example.com"></label>
              <label>API token <input id="api_token"></label>
            </div>
          </div>
          <div class="panel">
            <div class="panel-head"><div class="panel-title">Telegram and panel access</div></div>
            <div class="panel-body form-grid">
              <label>Telegram bot token <input id="bot_token"></label>
              <label>Admin chat ID <input id="admin_chat_id"></label>
              <label>Panel username <input id="admin_username"></label>
              <label>Panel password <input id="admin_password" type="password"></label>
            </div>
            <div class="panel-body actions">
              <button class="btn" onclick="saveSettings()">Save settings</button>
              <span id="status" class="toast"></span>
            </div>
          </div>
        </section>
      </div>
    </main>
  </div>

<script>
const $ = (id) => document.getElementById(id);
const fields = ["public_domain", "secret_domain", "acme_email", "api_token", "bot_token", "admin_chat_id", "admin_username", "admin_password"];
let currentUsers = [];

function adminUser() {
  return localStorage.getItem("np_admin_user") || $("login_user").value || "admin";
}

function adminPass() {
  return localStorage.getItem("np_admin_pass") || $("login_pass").value || "admin";
}

function setToast(message) {
  $("status").textContent = message || "";
  $("login_status").textContent = message || "";
  $("top_status").textContent = message ? "Attention" : "Online";
}

async function request(path, options = {}) {
  const headers = Object.assign({
    "X-Admin-Username": adminUser(),
    "X-Admin-Password": adminPass()
  }, options.headers || {});
  const res = await fetch(path, Object.assign({}, options, {headers}));
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status}: ${text}`);
  return text ? JSON.parse(text) : {};
}

function saveLogin() {
  localStorage.setItem("np_admin_user", $("login_user").value || "admin");
  localStorage.setItem("np_admin_pass", $("login_pass").value || "admin");
  loadAll();
}

function logout() {
  localStorage.removeItem("np_admin_user");
  localStorage.removeItem("np_admin_pass");
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login_user").value = "admin";
  $("login_pass").value = "admin";
}

function showView(name) {
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === name));
  document.querySelectorAll(".nav button").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === name));
  $("page_title").textContent = name[0].toUpperCase() + name.slice(1);
}

async function loadAll() {
  setToast("Loading...");
  try {
    $("login_user").value = adminUser();
    $("login_pass").value = adminPass();
    const settings = await request("/settings");
    fields.forEach((field) => $(field).value = settings[field] || "");
    await loadUsers();
    $("metric_domain").textContent = settings.public_domain || "-";
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
    setToast("");
  } catch (err) {
    $("app").classList.add("hidden");
    $("login").classList.remove("hidden");
    setToast(`Login failed: ${err.message}`);
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
    localStorage.setItem("np_admin_user", body.admin_username || "admin");
    localStorage.setItem("np_admin_pass", body.admin_password || "admin");
    $("login_user").value = body.admin_username || "admin";
    $("login_pass").value = body.admin_password || "admin";
    $("metric_domain").textContent = body.public_domain || "-";
    $("status").textContent = result.note || "Saved";
    $("top_status").textContent = "Online";
  } catch (err) {
    $("status").textContent = `Save failed: ${err.message}`;
    $("top_status").textContent = "Attention";
  }
}

async function loadUsers() {
  const result = await request("/users");
  currentUsers = result.users || [];
  $("metric_users").textContent = currentUsers.length;
  if (!currentUsers.length) {
    $("users").innerHTML = '<div class="empty">No active clients</div>';
    return;
  }
  const rows = currentUsers.map((name) => `
    <tr>
      <td>${escapeHtml(name)}</td>
      <td><span class="status-pill"><span class="dot"></span>enabled</span></td>
      <td>https://${escapeHtml(name)}:***@${escapeHtml($("public_domain").value || "domain")}</td>
      <td style="text-align:right"><button class="btn danger" onclick="kickUser('${escapeJs(name)}')">Kick</button></td>
    </tr>`).join("");
  $("users").innerHTML = `<table class="table"><thead><tr><th>Name</th><th>Status</th><th>Client URL</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function addUserFrom(inputId) {
  const input = $(inputId);
  const name = input.value.trim();
  if (!name) return;
  try {
    const result = await request(`/users/${encodeURIComponent(name)}`, {method: "POST"});
    $("created").innerHTML = `Created client URL:<span class="code">${escapeHtml(result.url)}</span>`;
    input.value = "";
    await loadUsers();
    showView("clients");
  } catch (err) {
    $("created").textContent = `Add failed: ${err.message}`;
  }
}

async function kickUser(name) {
  try {
    await request(`/users/${encodeURIComponent(name)}`, {method: "DELETE"});
    await loadUsers();
  } catch (err) {
    setToast(`Kick failed: ${err.message}`);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function escapeJs(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

window.addEventListener("load", () => {
  $("login_user").value = adminUser();
  $("login_pass").value = adminPass();
});
</script>
</body>
</html>"""
