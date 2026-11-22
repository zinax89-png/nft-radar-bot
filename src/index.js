const DEFAULT_SETTINGS = {
  new_gifts: true, nft: true, sales: true, images: true, links: true, prices: true
};

function emptyState() {
  return { users: [], offset: 0, gift_catalog: {}, portals_seen_sales: [], user_settings: {} };
}

function b64encode(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function b64decode(value) {
  const binary = atob(value.replace(/\n/g, ""));
  const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

async function loadState(env) {
  const url = "https://api.github.com/repos/" + env.GITHUB_REPOSITORY + "/contents/users.json";
  const response = await fetch(url, { headers: githubHeaders(env) });
  if (response.status === 404) return { state: emptyState(), sha: null };
  if (!response.ok) throw new Error("GitHub state read failed: " + response.status + " " + await response.text());
  const file = await response.json();
  const state = JSON.parse(b64decode(file.content));
  state.users = Array.isArray(state.users) ? state.users : [];
  state.gift_catalog = state.gift_catalog || {};
  state.portals_seen_sales = state.portals_seen_sales || [];
  state.user_settings = state.user_settings || {};
  return { state, sha: file.sha };
}

function githubHeaders(env) {
  return {
    "Authorization": "Bearer " + env.GITHUB_TOKEN,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
  };
}

async function saveState(env, state, sha) {
  const url = "https://api.github.com/repos/" + env.GITHUB_REPOSITORY + "/contents/users.json";
  const body = {
    message: "Save Global NFT Radar bot state",
    content: b64encode(JSON.stringify(state, null, 2))
  };
  if (sha) body.sha = sha;
  const response = await fetch(url, {
    method: "PUT",
    headers: { ...githubHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error("GitHub state save failed: " + response.status + " " + await response.text());
}

async function tg(env, method, payload = {}) {
  const response = await fetch("https://api.telegram.org/bot" + env.BOT_TOKEN + "/" + method, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!data.ok) console.log("Telegram API " + method + ": " + JSON.stringify(data));
  return data;
}

async function sendMessage(env, chatId, text, giftUrl) {
  const payload = { chat_id: chatId, text, disable_web_page_preview: false };
  if (giftUrl && /^https?:\/\//i.test(giftUrl)) {
    payload.reply_markup = { inline_keyboard: [[{ text: "🎁 Открыть подарок", url: giftUrl }]] };
  }
  const result = await tg(env, "sendMessage", payload);
  return !!result.ok;
}

async function sendPhoto(env, chatId, photo, caption, giftUrl) {
  const payload = { chat_id: chatId, photo, caption: caption.slice(0, 1024) };
  if (giftUrl && /^https?:\/\//i.test(giftUrl)) {
    payload.reply_markup = { inline_keyboard: [[{ text: "🎁 Открыть подарок", url: giftUrl }]] };
  }
  const result = await tg(env, "sendPhoto", payload);
  return !!result.ok;
}

function prefsFor(state, id) {
  return { ...DEFAULT_SETTINGS, ...(state.user_settings[String(id)] || {}) };
}

async function broadcastGift(env, state, text, options = {}) {
  let sent = 0;
  for (const id of state.users) {
    const prefs = prefsFor(state, id);
    if (!prefs[options.category || "new_gifts"]) continue;
    let message = text;
    if (!prefs.prices) message = message.split("\n").filter(line => !/цена:|💰/i.test(line)).join("\n");
    if (!prefs.links) message = message.split("\n").filter(line => !/https?:\/\/|🔗|🛒/i.test(line)).join("\n");
    const url = prefs.links ? options.url : null;
    let ok = false;
    if (options.photo && prefs.images) ok = await sendPhoto(env, id, options.photo, message, url);
    if (!ok) ok = await sendMessage(env, id, message, url);
    if (ok) sent++;
  }
  console.log("Notification sent to " + sent + "/" + state.users.length);
}

function extractUrl(obj, keys) {
  for (const source of [obj || {}, (obj && obj.nft) || {}]) {
    for (const key of keys) {
      const value = source[key];
      if (typeof value === "string" && /^https?:\/\//i.test(value)) return value;
    }
  }
  return null;
}

async function scanGifts(env, state) {
  const result = await tg(env, "getAvailableGifts");
  if (!result.ok) return;
  const gifts = (result.result && result.result.gifts) || [];
  const current = {};
  for (const gift of gifts) {
    const id = String(gift.id || "");
    if (!id) continue;
    current[id] = {
      star_count: gift.star_count,
      upgrade_star_count: gift.upgrade_star_count,
      total_count: gift.total_count,
      remaining_count: gift.remaining_count,
      unique_gift_variant_count: gift.unique_gift_variant_count,
      is_premium: !!gift.is_premium,
      sticker_emoji: gift.sticker && gift.sticker.emoji,
      publisher_chat: gift.publisher_chat
    };
  }
  if (!Object.keys(state.gift_catalog).length) {
    state.gift_catalog = current;
    console.log("Gift baseline stored: " + gifts.length);
    return;
  }
  for (const gift of gifts) {
    const id = String(gift.id || "");
    if (!id || state.gift_catalog[id]) continue;
    const sticker = gift.sticker || {};
    const direct = extractUrl(gift, ["url", "link", "gift_url", "nft_url", "item_url", "permalink"]);
    const lines = [
      (sticker.emoji || "🎁") + " Новый подарок Telegram",
      "ID: " + id,
      "💰 Цена: " + (gift.star_count == null ? "—" : gift.star_count) + " ⭐",
      direct ? "🔗 Открыть конкретный подарок: " + direct : "ℹ️ Telegram API не предоставил прямую ссылку на этот подарок.",
      "🛒 Маркет: https://portal-market.com"
    ];
    await broadcastGift(env, state, lines.join("\n"), {
      photo: null, url: direct, category: "new_gifts"
    });
    if (sticker.file_id) {
      // The text notification remains available even if Telegram cannot expose a direct item URL.
      for (const userId of state.users) {
        const p = prefsFor(state, userId);
        if (p.new_gifts && p.images) await tg(env, "sendSticker", { chat_id: userId, sticker: sticker.file_id });
      }
    }
  }
  state.gift_catalog = current;
}

function saleKey(action) {
  if (action.id != null) return String(action.id);
  const nft = action.nft || {};
  return [action.type, action.created_at, nft.id, action.amount, nft.name, nft.number].map(x => x || "").join("|");
}

async function scanPortals(env, state) {
  if (!env.PORTALS_API_KEY) {
    console.log("Portals monitor skipped: PORTALS_API_KEY missing");
    return;
  }
  const headers = { Authorization: "partners " + env.PORTALS_API_KEY };
  const known = new Set(state.portals_seen_sales || []);
  const baseline = known.size === 0;
  const found = [];
  let offset = 0;
  for (let page = 0; page < 35; page++) {
    const url = "https://portal-market.com/partners/market/actions/?limit=20&offset=" + offset;
    const response = await fetch(url, { headers });
    if (response.status === 503) return;
    if (response.status === 401 || response.status === 403) {
      console.log("Portals API access error: " + response.status);
      return;
    }
    if (!response.ok) {
      console.log("Portals API error: " + response.status + " " + (await response.text()).slice(0, 300));
      return;
    }
    const data = await response.json();
    const actions = Array.isArray(data.actions) ? data.actions : [];
    if (!actions.length) break;
    let reachedKnown = false;
    for (const action of actions) {
      if (action.type !== "purchase") continue;
      const key = saleKey(action);
      if (known.has(key)) reachedKnown = true;
      else found.push({ key, action });
    }
    if (reachedKnown || actions.length < 20) break;
    offset += 20;
  }
  if (baseline) {
    state.portals_seen_sales = found.map(x => x.key).slice(-2000);
    console.log("Portals baseline stored: " + state.portals_seen_sales.length);
    return;
  }
  for (const item of found.reverse()) {
    const action = item.action;
    const nft = action.nft || {};
    const name = nft.name || nft.title || nft.collection_name || "Telegram-подарок";
    const number = nft.number || nft.external_collection_number;
    const label = name + (number != null ? " #" + number : "");
    const direct = extractUrl(action, ["url", "link", "market_url", "marketplace_url", "permalink", "nft_url", "item_url", "external_url"]);
    const image = extractUrl(nft, ["image_url", "image", "imageUrl", "photo_url", "thumbnail", "preview", "cover"]);
    const lines = [
      "💸 Продажа Telegram-подарка",
      "🎁 " + label,
      "💰 Цена: " + (action.amount == null ? "—" : action.amount) + " GRAM",
      direct ? "🔗 Открыть конкретный лот: " + direct : "ℹ️ Portals API не передал прямую ссылку на этот лот.",
      "🛒 Маркет: https://portal-market.com"
    ];
    await broadcastGift(env, state, lines.join("\n"), { photo: image, url: direct, category: "sales" });
    known.add(item.key);
  }
  state.portals_seen_sales = Array.from(known).slice(-2000);
}

async function showSettings(env, state, chatId, messageId) {
  const prefs = prefsFor(state, chatId);
  const labels = {
    new_gifts: "🆕 Новые подарки", nft: "💎 Новые NFT", sales: "💸 Продажи NFT",
    images: "🖼 Изображения", links: "🔗 Ссылки", prices: "💰 Цены"
  };
  const keyboard = Object.entries(labels).map(([key, label]) => [{
    text: (prefs[key] ? "✅ " : "❌ ") + label, callback_data: "setting:" + key
  }]);
  keyboard.push([{ text: "🔄 Обновить", callback_data: "settings:refresh" }]);
  const payload = {
    chat_id: chatId,
    text: "⚙️ НАСТРОЙКИ УВЕДОМЛЕНИЙ\n\nНажимай на кнопки, чтобы включать или выключать уведомления:",
    reply_markup: { inline_keyboard: keyboard }
  };
  if (messageId) {
    payload.message_id = messageId;
    const edited = await tg(env, "editMessageText", payload);
    if (edited.ok) return;
  }
  await tg(env, "sendMessage", payload);
}

async function handleUpdate(env, state, update) {
  const callback = update.callback_query;
  if (callback) {
    const userId = String((callback.from || {}).id || "");
    if (callback.data === "settings:refresh") {
      await tg(env, "answerCallbackQuery", { callback_query_id: callback.id });
      await showSettings(env, state, userId, callback.message && callback.message.message_id);
    } else if (callback.data && callback.data.startsWith("setting:")) {
      const key = callback.data.slice(8);
      if (Object.prototype.hasOwnProperty.call(DEFAULT_SETTINGS, key)) {
        state.user_settings[userId] = { ...prefsFor(state, userId), [key]: !prefsFor(state, userId)[key] };
        await tg(env, "answerCallbackQuery", { callback_query_id: callback.id, text: "Настройка сохранена" });
        await showSettings(env, state, userId, callback.message && callback.message.message_id);
      }
    }
    return;
  }

  const message = update.message;
  if (!message || !message.text) return;
  const userId = String((message.from || {}).id || "");
  const text = message.text.trim();
  const command = text.split(/\s+/)[0].split("@")[0];

  if (command === "/start" || command === "/subscribe") {
    if (!state.users.includes(Number(userId))) state.users.push(Number(userId));
    state.user_settings[userId] = state.user_settings[userId] || { ...DEFAULT_SETTINGS };
    await sendMessage(env, userId, "🚀 Добро пожаловать в Global NFT Radar!\n\n✅ Ты подписан на уведомления о новых Telegram-подарках и продажах на Portals.\n\nКоманды: /help");
  } else if (command === "/stop" || command === "/unsubscribe") {
    state.users = state.users.filter(x => String(x) !== userId);
    await sendMessage(env, userId, "🔕 Ты отписался от уведомлений. Чтобы подписаться снова: /start");
  } else if (command === "/settings") {
    state.user_settings[userId] = state.user_settings[userId] || { ...DEFAULT_SETTINGS };
    await showSettings(env, state, userId);
  } else if (command === "/help") {
    await sendMessage(env, userId, "📡 GLOBAL NFT RADAR — команды\n\n/start или /subscribe — подписаться\n/stop или /unsubscribe — отписаться\n/status — статус подписки\n/test — тестовое уведомление\n/settings — настройки\n/help — справка\n/id — твой Telegram ID");
  } else if (command === "/status") {
    await sendMessage(env, userId, (state.users.some(x => String(x) === userId) ? "✅ Ты подписан." : "❌ Ты не подписан. Напиши /start.") + "\n\n👥 Всего подписчиков: " + state.users.length);
  } else if (command === "/id") {
    await sendMessage(env, userId, "Твой Telegram ID: " + userId);
  } else if (command === "/stats") {
    if (Number(userId) !== Number(env.ADMIN_ID || 0) || !env.ADMIN_ID) return;
    await sendMessage(env, userId, "📊 Статистика Global NFT Radar\n\nПодписчиков: " + state.users.length + "\nПодарков в каталоге: " + Object.keys(state.gift_catalog).length + "\nПродаж в базе: " + state.portals_seen_sales.length);
  } else if (command === "/test") {
    if (!state.users.some(x => String(x) === userId)) {
      await sendMessage(env, userId, "Сначала подпишись командой /start.");
      return;
    }
    await sendPhoto(env, userId, "https://telegram.org/img/t_logo.png", "🧪 ТЕСТОВОЕ УВЕДОМЛЕНИЕ GLOBAL NFT RADAR\n\nЕсли ты видишь это сообщение, бот успешно отправляет уведомления.");
  } else if (command === "/broadcast") {
    if (Number(userId) !== Number(env.ADMIN_ID || 0) || !env.ADMIN_ID) return;
    const announcement = text.slice(command.length).trim();
    if (!announcement) {
      await sendMessage(env, userId, "Использование: /broadcast текст");
      return;
    }
    let sent = 0;
    for (const id of state.users) if (await sendMessage(env, id, announcement)) sent++;
    await sendMessage(env, userId, "Рассылка завершена. Получателей: " + sent);
  }
}

async function registerTelegram(env, origin) {
  const webhook = origin + "/telegram-webhook";
  const commands = [
    { command: "start", description: "Подписаться на уведомления" },
    { command: "stop", description: "Отписаться от уведомлений" },
    { command: "subscribe", description: "Подписаться на уведомления" },
    { command: "unsubscribe", description: "Отписаться от уведомлений" },
    { command: "status", description: "Статус подписки" },
    { command: "settings", description: "Настройки уведомлений" },
    { command: "test", description: "Тестовое уведомление" },
    { command: "help", description: "Список команд" },
    { command: "id", description: "Показать мой Telegram ID" }
  ];
  const result = await tg(env, "setWebhook", {
    url: webhook,
    secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ["message", "callback_query"]
  });
  if (!result.ok) throw new Error("Telegram webhook setup failed");
  await tg(env, "setMyCommands", { commands });
}

async function handleWebhook(request, env) {
  if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }
  const update = await request.json();
  const { state, sha } = await loadState(env);
  await handleUpdate(env, state, update);
  await saveState(env, state, sha);
  return new Response("ok");
}

async function scheduled(env) {
  const { state, sha } = await loadState(env);
  await scanGifts(env, state);
  await scanPortals(env, state);
  await saveState(env, state, sha);
}

export default {
  async fetch(request, env) {
    const missing = ["BOT_TOKEN", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "WEBHOOK_SECRET"].filter(name => !env[name]);
    if (missing.length) {
      return new Response("Missing Worker configuration: " + missing.join(", "), {
        status: 500,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
    }
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/telegram-webhook") {
      try { return await handleWebhook(request, env); }
      catch (error) {
        console.log("Webhook error:", error && error.stack ? error.stack : error);
        return new Response("Webhook processing failed", { status: 500 });
      }
    }
    if (request.method === "GET") {
      try {
        await registerTelegram(env, url.origin);
        return new Response("Global NFT Radar is online. Telegram webhook configured.", {
          headers: { "Content-Type": "text/plain; charset=utf-8" }
        });
      } catch (error) {
        console.log("Setup error:", error && error.stack ? error.stack : error);
        return new Response("Setup failed. Check Worker logs and secrets.", { status: 500 });
      }
    }
    return new Response("Not found", { status: 404 });
  },
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(scheduled(env).catch(error => console.log("Scheduled scan error:", error && error.stack ? error.stack : error)));
  }
};
