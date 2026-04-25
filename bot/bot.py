import asyncio
import json
import os
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_URL = os.getenv("API_URL", "http://api:3000")
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
SETTINGS_FILE = STATE_DIR / "settings.json"


def load_settings() -> dict[str, str]:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def setting(name: str, env_name: str, default: str = "") -> str:
    return str(load_settings().get(name) or os.getenv(env_name, default))


settings = load_settings()
BOT_TOKEN = str(settings.get("bot_token") or BOT_TOKEN)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_admin(message: Message) -> bool:
    try:
        admin_chat_id = int(setting("admin_chat_id", "ADMIN_CHAT_ID", "0"))
    except ValueError:
        return False
    return message.chat.id == admin_chat_id


async def api_request(method: str, path: str) -> dict:
    api_token = setting("api_token", "API_TOKEN")
    if not api_token:
        raise RuntimeError("API token is not configured")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(method, f"{API_URL}{path}", headers={"X-API-Token": api_token})
    if response.status_code >= 300:
        raise RuntimeError(f"{response.status_code}: {response.text}")
    return response.json()


@dp.message(Command("start", "help"))
async def help_command(message: Message) -> None:
    if not is_admin(message):
        return
    await message.answer("/add name\n/list\n/kick name")


@dp.message(Command("add"))
async def add_user(message: Message) -> None:
    if not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Usage: /add name")
        return
    name = args[1].strip()
    try:
        result = await api_request("POST", f"/users/{name}")
    except Exception as exc:
        await message.answer(f"Add failed: {exc}")
        return

    await message.answer(
        "Created\n"
        f"User: {result['name']}\n"
        f"Password: {result['password']}\n"
        f"NaiveProxy URL:\n`{result['url']}`",
    )


@dp.message(Command("list"))
async def list_users(message: Message) -> None:
    if not is_admin(message):
        return
    try:
        result = await api_request("GET", "/users")
    except Exception as exc:
        await message.answer(f"List failed: {exc}")
        return

    users = result.get("users", [])
    if not users:
        await message.answer("No active accounts.")
        return
    await message.answer("\n".join(users))


@dp.message(Command("kick"))
async def kick_user(message: Message) -> None:
    if not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Usage: /kick name")
        return
    name = args[1].strip()
    try:
        await api_request("DELETE", f"/users/{name}")
    except Exception as exc:
        await message.answer(f"Kick failed: {exc}")
        return
    await message.answer(f"Deleted: {name}")


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
