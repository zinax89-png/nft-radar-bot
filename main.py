import os
import json
import base64
import logging

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_API = f"https://api.github.com/repos/{REPOSITORY}/contents/users.json"

users = set()
offset = 0
state_sha = None


async def load_state(session):
    global users, offset, state_sha

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with session.get(STATE_API, headers=headers) as response:
        if response.status == 404:
            users = set()
            offset = 0
            state_sha = None
            return
        response.raise_for_status()
        data = await response.json()

    state_sha = data["sha"]
    raw = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(raw)
    users = {int(user_id) for user_id in state.get("users", [])}
    offset = int(state.get("offset", 0))


async def save_state(session):
    global state_sha

    state = {
        "users": sorted(users),
        "offset": offset,
    }
    encoded = base64.b64encode(
        json.dumps(state, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    payload = {
        "message": "Save Global NFT Radar bot state",
        "content": encoded,
    }
    if state_sha:
        payload["sha"] = state_sha

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with session.put(
        STATE_API, headers=headers, json=payload
    ) as response:
        response.raise_for_status()
        result = await response.json()
        state_sha = result["content"]["sha"]


async def telegram_call(session, method, payload=None):
    async with session.post(
        f"{TELEGRAM_API}/{method}", json=payload or {}
    ) as response:
        data = await response.json()
        if not data.get("ok"):
            logging.warning("Telegram %s returned: %s", method, data)
        return data


async def send_message(session, chat_id, text):
    result = await telegram_call(
        session, "sendMessage", {"chat_id": chat_id, "text": text}
    )
    return bool(result.get("ok"))


async def handle_update(session, update):
    global offset

    offset = max(offset, int(update["update_id"]) + 1)
    message = update.get("message")
    if not message or not message.get("text"):
        return

    sender = message.get("from", {})
    user_id = int(sender.get("id", 0))
    text = message["text"].strip()
    command = text.split()[0].split("@")[0]

    if command == "/start":
        users.add(user_id)
        await send_message(
            session,
            user_id,
            "🚀 Добро пожаловать в Global NFT Radar!\n\n"
            "Ты подписан на уведомления о новых NFT.\n"
            "/stop — отписаться.\n"
            "/status — статус подписки.",
        )

    elif command == "/stop":
        users.discard(user_id)
        await send_message(session, user_id, "Ты отписался от уведомлений.")

    elif command == "/status":
        answer = (
            "✅ Ты подписан на уведомления."
            if user_id in users
            else "❌ Ты не подписан."
        )
        await send_message(session, user_id, answer)

    elif command == "/broadcast":
        if user_id != ADMIN_ID or ADMIN_ID == 0:
            return

        announcement = text.partition(" ")[2].strip()
        if not announcement:
            await send_message(session, user_id, "Использование: /broadcast текст")
            return

        sent = 0
        for subscriber_id in list(users):
            if await send_message(session, subscriber_id, announcement):
                sent += 1
        await send_message(
            session, user_id, f"Рассылка завершена. Получателей: {sent}"
        )


async def main():
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await load_state(session)

        result = await telegram_call(
            session,
            "getUpdates",
            {
                "offset": offset,
                "timeout": 0,
                "allowed_updates": ["message"],
            },
        )
        if not result.get("ok"):
            raise RuntimeError("Не удалось получить обновления Telegram")

        for update in result.get("result", []):
            await handle_update(session, update)

        await save_state(session)
        logging.info("Готово. Подписчиков: %d", len(users))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
