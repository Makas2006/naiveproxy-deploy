# NaiveProxy Caddy Deployment

This package builds Caddy v2 with `github.com/klzgrad/forwardproxy@naive`, exposes HTTPS/H2/H3 on port `443` for clients, and exposes the management API plus web panel on port `3000` by server IP.

## Files

- `caddy/Caddyfile` - Caddy v2 config with `forward_proxy`, `probe_resistance`, HTTP/2 and HTTP/3.
- `caddy/site.caddy` - generated/runtime-editable site config imported by Caddy.
- `caddy/users.caddy` - generated user list imported by Caddy.
- `api/app.py` - web panel and user/settings API with hot Caddy reload through the admin API.
- `bot/bot.py` - aiogram admin bot using the API.
- `scripts/install_debian.sh` - Docker install, BBR/fq tuning, nofile limits, and compose startup.

## Deploy on Debian

1. Point DNS `A/AAAA` records for `PUBLIC_DOMAIN` and `SECRET_DOMAIN` to the server.
2. Copy this directory to the server.
3. Run:

```sh
sudo bash scripts/install_debian.sh
```

4. Edit `.env` once for first boot:

```sh
nano .env
```

5. Start:

```sh
sudo docker compose up -d --build
```

After first boot, open the web panel by server IP:

```text
http://SERVER_IP:3000/
```

Use the `API_TOKEN` from `.env` for the first login. The domain is used only for client proxy connections on port `443`; the panel stays on port `3000` and does not need a domain. The panel can change:

- public domain
- secret probe domain
- ACME email
- API token
- Telegram bot token
- admin chat ID

Settings are stored in `state/settings.json`. Caddy-related changes are applied with hot reload. Changing the Telegram bot token requires restarting only the bot container:

```sh
sudo docker compose restart bot
```

## API

The API and panel are exposed on port `3000` by server IP and require `X-API-Token`.

If the server is public, restrict port `3000` with a firewall to your admin IP:

```sh
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw allow from YOUR_ADMIN_IP to any port 3000 proto tcp
```

```sh
curl -H "X-API-Token: $API_TOKEN" http://SERVER_IP:3000/users
curl -X POST -H "X-API-Token: $API_TOKEN" http://SERVER_IP:3000/users/alice
curl -X DELETE -H "X-API-Token: $API_TOKEN" http://SERVER_IP:3000/users/alice
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
