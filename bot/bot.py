import asyncio
import os

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
API_URL = os.getenv("API_URL", "http://api:3000")
API_TOKEN = os.getenv("API_TOKEN", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID is not configured")
if not API_TOKEN:
    raise RuntimeError("API_TOKEN is not configured")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_admin(message: Message) -> bool:
    return message.chat.id == ADMIN_CHAT_ID


async def api_request(method: str, path: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(method, f"{API_URL}{path}", headers={"X-API-Token": API_TOKEN})
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
