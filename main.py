import os
import json
import base64
import logging
import time
from urllib.parse import quote

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_API = f"https://api.github.com/repos/{REPOSITORY}/contents/users.json"
OPENSEA_EVENTS_API = "https://api.opensea.io/api/v2/events"

users = set()
offset = 0
state_sha = None
last_nft_scan = 0
seen_nft_events = []


async def load_state(session):
    global users, offset, state_sha, last_nft_scan, seen_nft_events

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
            last_nft_scan = int(time.time()) - 300
            seen_nft_events = []
            return
        response.raise_for_status()
        data = await response.json()

    state_sha = data["sha"]
    raw = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(raw)
    users = {int(user_id) for user_id in state.get("users", [])}
    offset = int(state.get("offset", 0))
    last_nft_scan = int(state.get("last_nft_scan", int(time.time()) - 300))
    seen_nft_events = list(state.get("seen_nft_events", []))[-500:]


async def save_state(session):
    global state_sha

    state = {
        "users": sorted(users),
        "offset": offset,
        "last_nft_scan": last_nft_scan,
        "seen_nft_events": seen_nft_events[-500:],
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
        session,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
    )
    return bool(result.get("ok"))


async def broadcast_nft(session, text):
    sent = 0
    for subscriber_id in list(users):
        if await send_message(session, subscriber_id, text):
            sent += 1
        await asyncio.sleep(0.05)
    logging.info("NFT alert delivered to %d/%d subscribers", sent, len(users))


def event_key(event):
    asset = event.get("nft") or event.get("asset") or {}
    contract = asset.get("contract") or {}
    if isinstance(contract, dict):
        address = contract.get("address", "")
    else:
        address = str(contract)
    return "|".join([
        str(event.get("chain", "")),
        address,
        str(asset.get("identifier", asset.get("token_id", ""))),
        str(event.get("transaction", "")),
        str(event.get("event_timestamp", "")),
    ])


def make_nft_message(event):
    asset = event.get("nft") or event.get("asset") or {}
    contract = asset.get("contract") or {}
    if isinstance(contract, dict):
        address = contract.get("address", "")
    else:
        address = str(contract)

    chain = str(event.get("chain") or "ethereum")
    token_id = str(asset.get("identifier") or asset.get("token_id") or "unknown")
    name = asset.get("name") or "Новый NFT"
    collection = asset.get("collection") or {}
    if isinstance(collection, dict):
        collection_name = collection.get("name") or collection.get("slug") or "Неизвестная коллекция"
    else:
        collection_name = str(collection or "Неизвестная коллекция")

    if address and token_id != "unknown":
        url = (
            "https://opensea.io/assets/"
            + quote(chain, safe="")
            + "/"
            + quote(address, safe="")
            + "/"
            + quote(token_id, safe="")
        )
        return (
            "🆕 Новый NFT (mint) обнаружен на OpenSea!\n\n"
            f"Коллекция: {collection_name}\n"
            f"NFT: {name}\n"
            f"ID: {token_id}\n"
            f"Сеть: {chain}\n\n"
            f"Открыть: {url}"
        )

    return (
        "🆕 Новое событие mint на OpenSea!\n"
        f"Коллекция: {collection_name}\n"
        f"NFT: {name}\n"
        f"Сеть: {chain}"
    )


async def scan_opensea(session):
    global last_nft_scan, seen_nft_events

    if not OPENSEA_API_KEY:
        logging.warning(
            "NFT monitoring is paused: add the OPENSEA_API_KEY Actions secret."
        )
        return

    now = int(time.time())
    after = max(0, last_nft_scan - 120)
    headers = {
        "accept": "application/json",
        "x-api-key": OPENSEA_API_KEY,
    }
    params = {
        "event_type": "mint",
        "after": after,
        "limit": 50,
    }

    new_seen = set(seen_nft_events)
    found = []
    cursor = None

    for _ in range(3):
        page_params = dict(params)
        if cursor:
            page_params["next"] = cursor

        async with session.get(
            OPENSEA_EVENTS_API, headers=headers, params=page_params
        ) as response:
            if response.status == 429:
                logging.warning("OpenSea rate limit reached; will retry next run.")
                return
            if response.status in (401, 403):
                logging.error(
                    "OpenSea API key rejected or not authorized (HTTP %s).",
                    response.status,
                )
                return
            response.raise_for_status()
            data = await response.json()

        events = data.get("asset_events", [])
        for event in events:
            key = event_key(event)
            if key not in new_seen:
                new_seen.add(key)
                found.append(event)

        cursor = data.get("next")
        if not cursor or len(events) < 50:
            break

    # Process oldest first so notifications arrive chronologically.
    found.sort(key=lambda item: int(item.get("event_timestamp", 0)))
    for event in found:
        if users:
            await broadcast_nft(session, make_nft_message(event))

    seen_nft_events = list(new_seen)[-500:]
    last_nft_scan = now
    logging.info("OpenSea scan complete. New mint events: %d", len(found))


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

        await scan_opensea(session)
        await save_state(session)
        logging.info("Готово. Подписчиков: %d", len(users))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
