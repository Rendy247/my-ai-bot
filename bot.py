#!/usr/bin/env python3
"""
Multi-Model AI Telegram Bot
Поддерживает несколько AI моделей через OpenAI-совместимые API
"""

import os
import json
import logging
import httpx
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Конфиг из переменных окружения ──────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
_admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS  = {int(x) for x in _admin_raw.split(",") if x.strip().isdigit()}

DATA_DIR      = Path("data")
MODELS_FILE   = DATA_DIR / "models.json"
HISTORY_FILE  = DATA_DIR / "history.json"
MAX_HISTORY   = 30   # сообщений на пользователя

# ── Состояния диалогов ───────────────────────────────────────────────────────
(
    ADD_NAME, ADD_BASE_URL, ADD_API_KEY, ADD_MODEL_ID,
    CHANGE_SELECT, CHANGE_FIELD, CHANGE_VALUE,
    DELETE_SELECT,
) = range(8)

# ════════════════════════════════════════════════════════════════════════════
#  Хранилище данных
# ════════════════════════════════════════════════════════════════════════════

def _load_json(path: Path, default) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_models() -> dict:
    return _load_json(MODELS_FILE, {"models": {}, "selections": {}})

def save_models(data: dict) -> None:
    _save_json(MODELS_FILE, data)

def load_history() -> dict:
    return _load_json(HISTORY_FILE, {})

def save_history(data: dict) -> None:
    _save_json(HISTORY_FILE, data)

# ════════════════════════════════════════════════════════════════════════════
#  Вспомогательные функции
# ════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_selected_model(user_id: int) -> dict | None:
    data   = load_models()
    models = data.get("models", {})
    name   = data.get("selections", {}).get(str(user_id))
    return models.get(name) if name else None

def models_keyboard(prefix: str = "sel") -> InlineKeyboardMarkup:
    data   = load_models()
    models = data.get("models", {})
    if not models:
        return InlineKeyboardMarkup([[InlineKeyboardButton("— пусто —", callback_data="noop")]])
    rows = [
        [InlineKeyboardButton(f"🤖 {name}", callback_data=f"{prefix}:{name}")]
        for name in models
    ]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

async def send_typing(update: Update) -> None:
    await update.effective_chat.send_action("typing")

def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]

# ════════════════════════════════════════════════════════════════════════════
#  Запрос к AI API
# ════════════════════════════════════════════════════════════════════════════

async def call_ai(model_cfg: dict, messages: list) -> str:
    url     = model_cfg["base_url"].rstrip("/") + "/chat/completions"
    api_key = model_cfg["api_key"]
    model   = model_cfg["model_id"]

    payload = {
        "model":    model,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ════════════════════════════════════════════════════════════════════════════
#  /start  /help
# ════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    admin_hint = ""
    if is_admin(update.effective_user.id):
        admin_hint = (
            "\n\n🔧 *Управление (только для вас):*\n"
            "/addmodel — добавить модель\n"
            "/changemodel — изменить API ключ / URL / ID модели\n"
            "/deletemodel — удалить модель"
        )
    text = (
        "👋 Привет! Я мульти-модельный AI бот.\n\n"
        "📋 *Команды:*\n"
        "/models — список и выбор модели\n"
        "/current — текущая модель\n"
        "/clear — очистить историю чата\n"
        "/help — помощь"
        + admin_hint
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)

# ════════════════════════════════════════════════════════════════════════════
#  /models — выбор модели
# ════════════════════════════════════════════════════════════════════════════

async def cmd_models(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data   = load_models()
    models = data.get("models", {})
    if not models:
        await update.message.reply_text(
            "😕 Моделей пока нет.\n"
            + ("Добавьте первую через /addmodel" if is_admin(update.effective_user.id) else "")
        )
        return
    await update.message.reply_text(
        "🤖 Выберите модель для общения:",
        reply_markup=models_keyboard("sel")
    )

async def cb_select_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, name = query.data.split(":", 1)

    data = load_models()
    if name not in data.get("models", {}):
        await query.edit_message_text("❌ Модель не найдена.")
        return

    uid = str(query.from_user.id)
    data.setdefault("selections", {})[uid] = name
    save_models(data)

    m = data["models"][name]
    await query.edit_message_text(
        f"✅ Выбрана модель: *{name}*\n"
        f"└ ID: `{m['model_id']}`\n"
        f"└ URL: `{m['base_url']}`\n\n"
        "Можете писать сообщения!",
        parse_mode="Markdown"
    )

# ════════════════════════════════════════════════════════════════════════════
#  /current
# ════════════════════════════════════════════════════════════════════════════

async def cmd_current(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = get_selected_model(update.effective_user.id)
    if not m:
        await update.message.reply_text("⚠️ Модель не выбрана. Используйте /models")
        return
    data = load_models()
    name = data["selections"].get(str(update.effective_user.id), "")
    await update.message.reply_text(
        f"🤖 Текущая модель: *{name}*\n"
        f"└ Model ID: `{m['model_id']}`\n"
        f"└ Base URL: `{m['base_url']}`\n"
        f"└ API Key: `{mask_key(m['api_key'])}`",
        parse_mode="Markdown"
    )

# ════════════════════════════════════════════════════════════════════════════
#  /clear
# ════════════════════════════════════════════════════════════════════════════

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid     = str(update.effective_user.id)
    history = load_history()
    history.pop(uid, None)
    save_history(history)
    await update.message.reply_text("🗑️ История чата очищена.")

# ════════════════════════════════════════════════════════════════════════════
#  Чат с моделью
# ════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    m   = get_selected_model(update.effective_user.id)

    if not m:
        await update.message.reply_text(
            "⚠️ Сначала выберите модель через /models"
        )
        return

    user_text = update.message.text.strip()
    history   = load_history()
    msgs      = history.get(uid, [])
    msgs.append({"role": "user", "content": user_text})

    # Обрезаем историю если слишком длинная
    if len(msgs) > MAX_HISTORY:
        msgs = msgs[-MAX_HISTORY:]

    await send_typing(update)

    try:
        reply = await call_ai(m, msgs)
    except httpx.HTTPStatusError as e:
        logger.error(f"API error: {e.response.status_code} {e.response.text}")
        await update.message.reply_text(
            f"❌ Ошибка API ({e.response.status_code}):\n`{e.response.text[:300]}`",
            parse_mode="Markdown"
        )
        return
    except Exception as e:
        logger.error(f"Error calling AI: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    msgs.append({"role": "assistant", "content": reply})
    history[uid] = msgs
    save_history(history)

    # Отправляем ответ (с фолбэком если Markdown не парсится)
    try:
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(reply)

# ════════════════════════════════════════════════════════════════════════════
#  /addmodel  (только админ)
# ════════════════════════════════════════════════════════════════════════════

async def cmd_addmodel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    await update.message.reply_text(
        "➕ *Добавление модели*\n\n"
        "Шаг 1/4 — Введите *название* модели (как вы её будете называть):\n"
        "Например: `Groq Llama`, `OpenRouter GPT`\n\n"
        "Отправьте /cancel для отмены.",
        parse_mode="Markdown"
    )
    return ADD_NAME

async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return ADD_NAME

    data = load_models()
    if name in data.get("models", {}):
        await update.message.reply_text(
            f"⚠️ Модель *{name}* уже существует. Введите другое название:",
            parse_mode="Markdown"
        )
        return ADD_NAME

    ctx.user_data["new_model"] = {"name": name}
    await update.message.reply_text(
        f"✅ Название: *{name}*\n\n"
        "Шаг 2/4 — Введите *Base URL* API:\n"
        "Примеры:\n"
        "• `https://api.groq.com/openai/v1`\n"
        "• `https://openrouter.ai/api/v1`\n"
        "• `https://api.together.xyz/v1`\n"
        "• `https://generativelanguage.googleapis.com/v1beta/openai`",
        parse_mode="Markdown"
    )
    return ADD_BASE_URL

async def add_base_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip().rstrip("/")
    if not url.startswith("http"):
        await update.message.reply_text("❌ URL должен начинаться с http:// или https://")
        return ADD_BASE_URL

    ctx.user_data["new_model"]["base_url"] = url
    await update.message.reply_text(
        f"✅ URL: `{url}`\n\n"
        "Шаг 3/4 — Введите *API ключ*:",
        parse_mode="Markdown"
    )
    return ADD_API_KEY

async def add_api_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    if not key:
        await update.message.reply_text("❌ API ключ не может быть пустым.")
        return ADD_API_KEY

    # Удаляем сообщение с ключом для безопасности
    try:
        await update.message.delete()
    except Exception:
        pass

    ctx.user_data["new_model"]["api_key"] = key
    await update.effective_chat.send_message(
        f"✅ API ключ: `{mask_key(key)}`\n\n"
        "Шаг 4/4 — Введите *ID модели* (строка для API):\n"
        "Примеры:\n"
        "• `llama3-8b-8192`\n"
        "• `gpt-4o-mini`\n"
        "• `mistral-7b-instruct`\n"
        "• `gemini-1.5-flash`",
        parse_mode="Markdown"
    )
    return ADD_MODEL_ID

async def add_model_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    model_id = update.message.text.strip()
    if not model_id:
        await update.message.reply_text("❌ ID модели не может быть пустым.")
        return ADD_MODEL_ID

    nm = ctx.user_data["new_model"]
    nm["model_id"] = model_id

    data = load_models()
    data.setdefault("models", {})[nm["name"]] = {
        "base_url": nm["base_url"],
        "api_key":  nm["api_key"],
        "model_id": nm["model_id"],
    }
    save_models(data)

    await update.message.reply_text(
        f"🎉 Модель *{nm['name']}* добавлена!\n\n"
        f"└ Model ID: `{nm['model_id']}`\n"
        f"└ Base URL: `{nm['base_url']}`\n"
        f"└ API Key: `{mask_key(nm['api_key'])}`\n\n"
        "Выберите её через /models",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  /deletemodel  (только админ)
# ════════════════════════════════════════════════════════════════════════════

async def cmd_deletemodel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    data   = load_models()
    models = data.get("models", {})
    if not models:
        await update.message.reply_text("😕 Моделей нет.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🗑️ *Удаление модели*\nВыберите модель для удаления:",
        reply_markup=models_keyboard("del"),
        parse_mode="Markdown"
    )
    return DELETE_SELECT

async def cb_delete_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, name = query.data.split(":", 1)

    data = load_models()
    if name not in data.get("models", {}):
        await query.edit_message_text("❌ Модель не найдена.")
        return ConversationHandler.END

    del data["models"][name]
    # Убираем выбор этой модели у всех пользователей
    data["selections"] = {
        uid: sel for uid, sel in data.get("selections", {}).items()
        if sel != name
    }
    save_models(data)

    await query.edit_message_text(f"✅ Модель *{name}* удалена.", parse_mode="Markdown")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  /changemodel — изменить параметры модели  (только админ)
# ════════════════════════════════════════════════════════════════════════════

async def cmd_changemodel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    data   = load_models()
    models = data.get("models", {})
    if not models:
        await update.message.reply_text("😕 Моделей нет.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✏️ *Изменение модели*\nВыберите модель:",
        reply_markup=models_keyboard("chg"),
        parse_mode="Markdown"
    )
    return CHANGE_SELECT

async def cb_change_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, name = query.data.split(":", 1)

    data = load_models()
    if name not in data.get("models", {}):
        await query.edit_message_text("❌ Модель не найдена.")
        return ConversationHandler.END

    ctx.user_data["change_model"] = name
    m = data["models"][name]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 API Key",   callback_data="field:api_key")],
        [InlineKeyboardButton("🌐 Base URL",  callback_data="field:base_url")],
        [InlineKeyboardButton("🏷️ Model ID",  callback_data="field:model_id")],
        [InlineKeyboardButton("❌ Отмена",     callback_data="cancel")],
    ])
    await query.edit_message_text(
        f"✏️ Модель: *{name}*\n"
        f"└ Model ID: `{m['model_id']}`\n"
        f"└ Base URL: `{m['base_url']}`\n"
        f"└ API Key: `{mask_key(m['api_key'])}`\n\n"
        "Что изменить?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return CHANGE_FIELD

async def cb_change_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, field = query.data.split(":", 1)

    ctx.user_data["change_field"] = field
    labels = {"api_key": "API Key", "base_url": "Base URL", "model_id": "Model ID"}

    await query.edit_message_text(
        f"✏️ Введите новое значение для *{labels[field]}*:",
        parse_mode="Markdown"
    )
    return CHANGE_VALUE

async def change_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    name  = ctx.user_data.get("change_model")
    field = ctx.user_data.get("change_field")

    if not value:
        await update.message.reply_text("❌ Значение не может быть пустым.")
        return CHANGE_VALUE

    data = load_models()
    if name not in data.get("models", {}):
        await update.message.reply_text("❌ Модель не найдена.")
        return ConversationHandler.END

    if field == "api_key":
        try:
            await update.message.delete()
        except Exception:
            pass

    data["models"][name][field] = value
    save_models(data)

    display = mask_key(value) if field == "api_key" else value
    await update.effective_chat.send_message(
        f"✅ Обновлено!\n"
        f"*{name}* → `{field}` = `{display}`",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  /listmodels — просмотр всех моделей  (для всех)
# ════════════════════════════════════════════════════════════════════════════

async def cmd_listmodels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data   = load_models()
    models = data.get("models", {})
    if not models:
        await update.message.reply_text("😕 Моделей нет.")
        return

    lines = [f"📋 *Доступные модели ({len(models)}):*\n"]
    for name, m in models.items():
        lines.append(
            f"🤖 *{name}*\n"
            f"   └ Model ID: `{m['model_id']}`\n"
            f"   └ Base URL: `{m['base_url']}`\n"
            f"   └ API Key: `{mask_key(m['api_key'])}`\n"
        )
    lines.append("\nДля выбора: /models")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════════════════
#  Общие хендлеры (cancel, noop)
# ════════════════════════════════════════════════════════════════════════════

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END

async def cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx.user_data.clear()
    await query.edit_message_text("❌ Отменено.")
    return ConversationHandler.END

async def cb_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()

# ════════════════════════════════════════════════════════════════════════════
#  Запуск
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Установите переменную окружения BOT_TOKEN.")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не заданы — никто не сможет управлять моделями!")

    app = Application.builder().token(BOT_TOKEN).build()

    # ── /addmodel conversation ───────────────────────────────────────────────
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("addmodel", cmd_addmodel)],
        states={
            ADD_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_BASE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_base_url)],
            ADD_API_KEY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_api_key)],
            ADD_MODEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_model_id)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # ── /changemodel conversation ────────────────────────────────────────────
    change_conv = ConversationHandler(
        entry_points=[CommandHandler("changemodel", cmd_changemodel)],
        states={
            CHANGE_SELECT: [CallbackQueryHandler(cb_change_select, pattern=r"^chg:")],
            CHANGE_FIELD:  [CallbackQueryHandler(cb_change_field,  pattern=r"^field:")],
            CHANGE_VALUE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, change_value)],
        },
        fallbacks=[
            CommandHandler("cancel",        cmd_cancel),
            CallbackQueryHandler(cb_cancel, pattern=r"^cancel$"),
        ],
    )

    # ── /deletemodel conversation ────────────────────────────────────────────
    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("deletemodel", cmd_deletemodel)],
        states={
            DELETE_SELECT: [CallbackQueryHandler(cb_delete_model, pattern=r"^del:")],
        },
        fallbacks=[
            CommandHandler("cancel",        cmd_cancel),
            CallbackQueryHandler(cb_cancel, pattern=r"^cancel$"),
        ],
    )

    # ── Регистрируем всё ─────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("models",      cmd_models))
    app.add_handler(CommandHandler("listmodels",  cmd_listmodels))
    app.add_handler(CommandHandler("current",     cmd_current))
    app.add_handler(CommandHandler("clear",       cmd_clear))
    app.add_handler(add_conv)
    app.add_handler(change_conv)
    app.add_handler(delete_conv)

    # Inline-кнопки выбора модели для /models
    app.add_handler(CallbackQueryHandler(cb_select_model, pattern=r"^sel:"))
    app.add_handler(CallbackQueryHandler(cb_noop,         pattern=r"^noop$"))

    # Сообщения — чат с AI
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущен. Ожидаю сообщения...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
