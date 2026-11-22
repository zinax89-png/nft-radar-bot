import os
import json
import base64
import logging
import time
import asyncio

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")
PORTALS_API_KEY = os.getenv("PORTALS_API_KEY", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_API = f"https://api.github.com/repos/{REPOSITORY}/contents/users.json"

users = set()
offset = 0
state_sha = None
gift_catalog = {}
portals_seen_sales = []


async def load_state(session):
    global users, offset, state_sha, gift_catalog, portals_seen_sales

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
            gift_catalog = {}
            portals_seen_sales = []
            return
        response.raise_for_status()
        data = await response.json()

    state_sha = data["sha"]
    raw = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(raw)
    users = {int(user_id) for user_id in state.get("users", [])}
    offset = int(state.get("offset", 0))
    gift_catalog = {str(k): v for k, v in state.get("gift_catalog", {}).items()}
    portals_seen_sales = [str(x) for x in state.get("portals_seen_sales", [])]


async def save_state(session):
    global state_sha

    state = {
        "users": sorted(users),
        "offset": offset,
        "gift_catalog": gift_catalog,
        "portals_seen_sales": portals_seen_sales[-2000:],
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


# Telegram Gifts catalog monitoring through the official Bot API.
# getAvailableGifts returns gifts currently available to this bot.
gift_catalog = {}

def gift_description(gift):
    sticker = gift.get('sticker') or {}
    emoji = sticker.get('emoji') or '🎁'
    gift_id = str(gift.get('id', 'unknown'))
    lines = ['🎁 Новый подарок Telegram появился в каталоге!', '', 'Подарок: ' + emoji, 'ID: ' + gift_id, 'Цена: ' + str(gift.get('star_count', '—')) + ' ⭐']
    if gift.get('upgrade_star_count') is not None:
        lines.append('Улучшение до коллекционного: ' + str(gift['upgrade_star_count']) + ' ⭐')
    if gift.get('total_count') is not None:
        lines.append('Тираж: ' + str(gift['total_count']))
    if gift.get('remaining_count') is not None:
        lines.append('Осталось: ' + str(gift['remaining_count']))
    if gift.get('unique_gift_variant_count') is not None:
        lines.append('Вариантов коллекционного подарка: ' + str(gift['unique_gift_variant_count']))
    if gift.get('is_premium'):
        lines.append('Доступен только Telegram Premium')
    publisher = gift.get('publisher_chat') or {}
    if publisher:
        lines.append('Издатель: ' + str(publisher.get('title') or publisher.get('username') or 'Канал'))
    lines.extend(['', 'Каталог подарков Telegram: https://t.me/gifts'])
    return "\n".join(lines)

async def scan_telegram_gifts(session):
    global gift_catalog
    result = await telegram_call(session, 'getAvailableGifts')
    if not result.get('ok'):
        logging.error('Could not fetch Telegram gifts catalog.')
        return
    gifts = result.get('result', {}).get('gifts', [])
    current = {}
    for gift in gifts:
        gift_id = str(gift.get('id', ''))
        if not gift_id:
            continue
        current[gift_id] = {'star_count': gift.get('star_count'), 'upgrade_star_count': gift.get('upgrade_star_count'), 'total_count': gift.get('total_count'), 'remaining_count': gift.get('remaining_count'), 'unique_gift_variant_count': gift.get('unique_gift_variant_count'), 'is_premium': bool(gift.get('is_premium')), 'sticker_emoji': (gift.get('sticker') or {}).get('emoji'), 'publisher_chat': gift.get('publisher_chat')}
    if not gift_catalog:
        gift_catalog = current
        logging.info('Telegram gifts baseline saved: %d gifts.', len(current))
        return
    new_ids = [gift_id for gift_id in current if gift_id not in gift_catalog]
    for gift_id in new_ids:
        gift = next(item for item in gifts if str(item.get('id')) == gift_id)
        if users:
            await broadcast_nft(session, gift_description(gift))
    gift_catalog = current
    logging.info('Telegram gifts scan complete. Catalog: %d; new gifts: %d', len(current), len(new_ids))


# Portals marketplace completed-sale monitor.
# Requires PORTALS_API_KEY (GitHub Actions secret). The free API tier is
# documented as 1 request/second. This feed covers Portals only.
def portals_sale_key(action):
    if action.get("id") is not None:
        return str(action["id"])
    nft = action.get("nft") or {}
    return "|".join(str(x or "") for x in (
        action.get("type"), action.get("created_at"), nft.get("id"),
        action.get("amount"), nft.get("name"), nft.get("number")
    ))

def portals_sale_message(action):
    nft = action.get("nft") or {}
    attrs = nft.get("attributes") or []
    traits = {}
    for attr in attrs:
        if isinstance(attr, dict):
            traits[str(attr.get("type") or attr.get("trait_type") or "Атрибут")] = attr.get("value")
    name = nft.get("name") or nft.get("title") or nft.get("collection_name") or "Telegram-подарок"
    number = nft.get("number") or nft.get("external_collection_number")
    model = traits.get("Model") or traits.get("model")
    backdrop = traits.get("Backdrop") or traits.get("backdrop")
    symbol = traits.get("Symbol") or traits.get("symbol")
    lines = ["💸 НОВАЯ ПРОДАЖА TELEGRAM-ПОДАРКА", "", "🎁 " + str(name)]
    if number is not None:
        lines.append("Номер: #" + str(number))
    if model:
        lines.append("Модель: " + str(model))
    if backdrop:
        lines.append("Фон: " + str(backdrop))
    if symbol:
        lines.append("Символ: " + str(symbol))
    if action.get("amount") is not None:
        lines.append("Цена: " + str(action["amount"]) + " GRAM")
    if action.get("created_at"):
        lines.append("Время: " + str(action["created_at"]))
    lines.append("Площадка: Portals")
    lines.append("")
    lines.append("Источник: https://portal-market.com")
    return "\\n".join(lines)

async def scan_portals_sales(session):
    global portals_seen_sales
    if not PORTALS_API_KEY:
        logging.info("Portals sales monitor skipped: PORTALS_API_KEY is not configured.")
        return

    headers = {"Authorization": "partners " + PORTALS_API_KEY}
    url = "https://portal-market.com/partners/market/actions/"
    known = set(portals_seen_sales)
    found = []
    offset_value = 0
    is_baseline = not portals_seen_sales

    # Read newest completed purchases, paging until we reach already-seen data.
    for page in range(35):  # API documents offsets up to 700, 20 actions/page.
        params = {"action_types": "purchase", "limit": 20, "offset": offset_value, "sort_by": "created_at desc"}
        async with session.get(url, headers=headers, params=params) as response:
            if response.status == 503:
                logging.warning("Portals market feed temporarily unavailable (503).")
                return
            if response.status in (401, 403):
                body = await response.text()
                logging.error("Portals API access error %s: %s", response.status, body[:500])
                return
            response.raise_for_status()
            data = await response.json()

        actions = data.get("actions", []) if isinstance(data, dict) else []
        if not actions:
            break
        page_new = []
        reached_known = False
        for action in actions:
            key = portals_sale_key(action)
            if key in known:
                reached_known = True
                continue
            page_new.append((key, action))
        found.extend(page_new)
        if reached_known or len(actions) < 20:
            break
        offset_value += 20
        await asyncio.sleep(1.05)  # respect the documented free-tier rate limit

    if is_baseline:
        # First run stores current activity and does not send historical sales.
        portals_seen_sales = list(dict.fromkeys(portals_sale_key(a) for a in (
            action for _, action in found
        )))
        logging.info("Portals sales baseline saved: %d recent purchases.", len(portals_seen_sales))
        return

    for key, action in reversed(found):
        if users:
            await broadcast_nft(session, portals_sale_message(action))
        known.add(key)
    portals_seen_sales = list(known)[-2000:]
    logging.info("Portals sales scan complete. New purchases: %d", len(found))


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

        await scan_telegram_gifts(session)
        await scan_portals_sales(session)
        await save_state(session)
        logging.info("Готово. Подписчиков: %d", len(users))


if __name__ == "__main__":
    asyncio.run(main())
