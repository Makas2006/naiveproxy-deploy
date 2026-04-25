# NaiveProxy Caddy Deployment

This package builds Caddy v2 with `github.com/klzgrad/forwardproxy@naive`, exposes HTTPS/H2/H3 on port `443`, and runs a local management API on `127.0.0.1:3000`.

## Files

- `caddy/Caddyfile` - Caddy v2 config with `forward_proxy`, `probe_resistance`, HTTP/2 and HTTP/3.
- `caddy/users.caddy` - generated user list imported by Caddy.
- `api/app.py` - user management API with hot Caddy reload through the admin API.
- `bot/bot.py` - aiogram admin bot using the API.
- `scripts/install_debian.sh` - Docker install, BBR/fq tuning, nofile limits, and compose startup.

## Deploy on Debian

1. Point DNS `A/AAAA` records for `PUBLIC_DOMAIN` and `SECRET_DOMAIN` to the server.
2. Copy this directory to the server.
3. Run:

```sh
sudo bash scripts/install_debian.sh
```

4. Edit `.env`:

```sh
nano .env
```

5. Start:

```sh
sudo docker compose up -d --build
```

## API

The API is bound to localhost only.

```sh
curl -H "X-API-Token: $API_TOKEN" http://127.0.0.1:3000/users
curl -X POST -H "X-API-Token: $API_TOKEN" http://127.0.0.1:3000/users/alice
curl -X DELETE -H "X-API-Token: $API_TOKEN" http://127.0.0.1:3000/users/alice
```

Adding or deleting a user rewrites `caddy/users.caddy` and sends a hot reload to Caddy without restarting the process.

## Telegram

Bot commands, available only to `ADMIN_CHAT_ID`:

- `/add name`
- `/list`
- `/kick name`

The generated client URL has this format:

```text
https://user:password@proxy.example.com
```

For QUIC-capable clients you can manually use:

```text
quic://user:password@proxy.example.com
```
