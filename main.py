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

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_API = f"https://api.github.com/repos/{REPOSITORY}/contents/users.json"

users = set()
offset = 0
state_sha = None
last_nft_scan = 0
seen_nft_events = []
chain_blocks = {}


async def load_state(session):
    global users, offset, state_sha, last_nft_scan, seen_nft_events, chain_blocks

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
            chain_blocks = {}
            return
        response.raise_for_status()
        data = await response.json()

    state_sha = data["sha"]
    raw = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(raw)
    users = {int(user_id) for user_id in state.get("users", [])}
    offset = int(state.get("offset", 0))
    last_nft_scan = int(state.get("last_nft_scan", int(time.time()) - 300))
    seen_nft_events = list(state.get("seen_nft_events", []))[-1000:]
    chain_blocks = {str(k): int(v) for k, v in state.get("chain_blocks", {}).items()}


async def save_state(session):
    global state_sha

    state = {
        "users": sorted(users),
        "offset": offset,
        "last_nft_scan": last_nft_scan,
        "seen_nft_events": seen_nft_events[-1000:],
        "chain_blocks": chain_blocks,
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


# PublicNode exposes public JSON-RPC endpoints; no API key is needed.
CHAINS = {
    "ethereum": {
        "rpc": "https://ethereum.publicnode.com",
        "explorer": "https://etherscan.io",
        "opensea": "ethereum",
    },
    "base": {
        "rpc": "https://base.publicnode.com",
        "explorer": "https://basescan.org",
        "opensea": "base",
    },
    "polygon": {
        "rpc": "https://polygon-bor-rpc.publicnode.com",
        "explorer": "https://polygonscan.com",
        "opensea": "matic",
    },
}

# ERC-721 Transfer, ERC-1155 TransferSingle and TransferBatch.
ZERO_ADDRESS_TOPIC = "0x" + "0" * 64
MINT_FILTERS = [
    # ERC-721 Transfer: indexed "from" is topic 1.
    ("erc721", ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", ZERO_ADDRESS_TOPIC]),
    # ERC-1155: indexed "from" is topic 2.
    ("erc1155", ["0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62", None, ZERO_ADDRESS_TOPIC]),
    ("erc1155", ["0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb", None, ZERO_ADDRESS_TOPIC]),
]


def log_event_key(chain, log):
    return f"{chain}:{log.get('transactionHash', '')}:{log.get('logIndex', '')}"


def make_chain_mint_message(chain, log):
    contract = log.get("address", "")
    tx_hash = log.get("transactionHash", "")
    token_id = "не указан"
    topics = log.get("topics", [])
    # ERC-721 token ID is topic 3; ERC-1155 token ID is the first data word.
    try:
        if len(topics) >= 4:
            token_id = str(int(topics[3], 16))
        elif log.get("data") and log["data"] != "0x":
            token_id = str(int(log["data"][2:66], 16))
    except (TypeError, ValueError):
        pass

    cfg = CHAINS[chain]
    marketplace_url = (
        f"https://opensea.io/assets/{cfg['opensea']}/{contract}"
        + (f"/{token_id}" if token_id != "не указан" else "")
    )
    explorer_url = f"{cfg['explorer']}/tx/{tx_hash}" if tx_hash else cfg["explorer"]
    return (
        f"🆕 Новый NFT mint в сети {chain.title()}!\\n\\n"
        f"Контракт: {contract}\\n"
        f"Token ID: {token_id}\\n"
        f"Транзакция: {explorer_url}\\n"
        f"Открыть на OpenSea: {marketplace_url}"
    )


async def rpc_call(session, url, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(url, json=payload) as response:
        response.raise_for_status()
        data = await response.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data.get("result")


async def scan_chain(session, chain):
    global chain_blocks, seen_nft_events

    cfg = CHAINS[chain]
    rpc = cfg["rpc"]
    latest_hex = await rpc_call(session, rpc, "eth_blockNumber", [])
    latest = int(latest_hex, 16)

    # Start close to the chain head on first run; do not send old historical mints.
    cursor = chain_blocks.get(chain)
    if cursor is None:
        chain_blocks[chain] = max(0, latest - 3)
        logging.info("%s initialized at block %d", chain, chain_blocks[chain])
        return

    start_block = cursor + 1
    if start_block > latest:
        return

    # Small ranges keep public RPC requests lightweight and reduce provider limits.
    new_seen = set(seen_nft_events)
    notifications = []
    while start_block <= latest:
        end_block = min(start_block + 499, latest)
        for event_kind, topics in MINT_FILTERS:
            logs = await rpc_call(
                session,
                rpc,
                "eth_getLogs",
                [{
                    "fromBlock": hex(start_block),
                    "toBlock": hex(end_block),
                    "topics": topics,
                }],
            )
            for log in logs or []:
                # ERC-20 uses the same Transfer signature as ERC-721, but has only 3 topics.
                if event_kind == "erc721" and len(log.get("topics", [])) != 4:
                    continue
                key = log_event_key(chain, log)
                if key not in new_seen:
                    new_seen.add(key)
                    notifications.append((int(log.get("blockNumber", "0x0"), 16),
                                          int(log.get("logIndex", "0x0"), 16),
                                          make_chain_mint_message(chain, log)))

        start_block = end_block + 1

    notifications.sort(key=lambda item: (item[0], item[1]))
    for _, _, message in notifications:
        if users:
            await broadcast_nft(session, message)

    chain_blocks[chain] = latest
    seen_nft_events = list(new_seen)[-1000:]
    logging.info("%s scan complete through block %d; new mints: %d",
                 chain, latest, len(notifications))


async def scan_nfts(session):
    for chain in CHAINS:
        try:
            await scan_chain(session, chain)
        except Exception:
            # Keep the previous cursor so this chain is retried on the next run.
            logging.exception("NFT scan failed for %s", chain)


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

        await scan_nfts(session)
        await save_state(session)
        logging.info("Готово. Подписчиков: %d", len(users))


if __name__ == "__main__":
    asyncio.run(main())
