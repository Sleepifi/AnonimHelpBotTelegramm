import json
import logging
import os
import sys
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anon_bot")

# ===== Настройки =====

# Режим можно передать аргументом: python main.py main | python main.py test
MODES = {"main": "bot_main", "test": "bot_test"}
mode = sys.argv[1] if len(sys.argv) > 1 else input("Введите режим (main/test):\n").strip()
if mode not in MODES:
    raise SystemExit("Неверный режим: используйте 'main' или 'test'")

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), MODES[mode])
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise SystemExit(f"TOKEN не найден в {BASE_DIR}/.env")
DATA_FILE = os.path.join(BASE_DIR, os.getenv("DATA_FILE", "db.json"))

HIGH_ADMINS = {5046560155, 1513168841}

log.info("Режим: %s, база: %s", mode, DATA_FILE)


# ===== База данных (json) =====

def load_data() -> dict:
    # Если файла нет — начинаем с пустой базы.
    # Если файл ПОВРЕЖДЁН — падаем с ошибкой, а не затираем его пустой базой.
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except FileNotFoundError:
        db = {}
    db.setdefault("clients", {})
    if not isinstance(db.get("admins"), list):
        db["admins"] = []
    return db


def save_data() -> None:
    # Пишем во временный файл и подменяем — так база не побьётся при сбое.
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, DATA_FILE)


data = load_data()
bot = Bot(token=TOKEN)
dp = Dispatcher()


# ===== Вспомогательные функции =====

def is_admin(tg_id: int) -> bool:
    return tg_id in data["admins"]


def find_cid(tg_id: int) -> str | None:
    """Анонимный номер пользователя по его telegram id (или None)."""
    for cid, info in data["clients"].items():
        if info["tg_id"] == tg_id:
            return cid
    return None


def get_or_create_cid(tg_id: int) -> str:
    cid = find_cid(tg_id)
    if cid is None:
        cid = str(max(map(int, data["clients"]), default=0) + 1)
        data["clients"][cid] = {"tg_id": tg_id, "admin": None, "user": None, "username": None}
        save_data()
    return cid


def parse_id_arg(msg: Message) -> int | None:
    """Достаёт число из команды вида '/cmd 123'."""
    parts = (msg.text or "").split()
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def take_keyboard(cid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Взять #{cid}", callback_data=f"take:{cid}")
    return kb.as_markup()


# Связи между сообщениями в разных чатах: (чат, id сообщения) -> (другой чат, id копии).
# Нужны, чтобы «ответ» (reply) в Telegram превращался в ответ на нужное сообщение
# у собеседника. Хранится в памяти (последние LINKS_LIMIT штук), после перезапуска бота
# ответы на старые сообщения уже не будут «цепляться».
LINKS_LIMIT = 10_000
links: OrderedDict[tuple[int, int], tuple[int, int]] = OrderedDict()


def add_link(src: tuple[int, int], dst: tuple[int, int], both: bool = True) -> None:
    links[dst] = src
    if both:
        links[src] = dst
    while len(links) > LINKS_LIMIT:
        links.popitem(last=False)


async def relay(msg: Message, chat_id: int, prefix: str = "",
                reply_markup: InlineKeyboardMarkup | None = None,
                both_ways: bool = True) -> bool:
    """
    Передать сообщение любого типа (текст, фото, видео, голосовое, файл,
    стикер, кружок и т.д.) другому человеку.
    Отправитель остаётся анонимным: copy_message не показывает, от кого оно.
    prefix добавляется к тексту или подписи (у стикеров подписи нет — там без префикса).
    Если отправитель ответил (reply) на сообщение, то у получателя копия
    тоже будет ответом на соответствующее сообщение.
    both_ways=False — когда одно сообщение уходит сразу нескольким получателям.
    """
    reply_params = None
    if msg.reply_to_message:
        target = links.get((msg.chat.id, msg.reply_to_message.message_id))
        if target and target[0] == chat_id:
            reply_params = ReplyParameters(message_id=target[1],
                                           allow_sending_without_reply=True)
    try:
        if msg.text:
            sent = await bot.send_message(chat_id, prefix + msg.text,
                                          reply_markup=reply_markup,
                                          reply_parameters=reply_params)
        else:
            extra = {"caption": (prefix + (msg.caption or ""))[:1024]} if prefix else {}
            sent = await msg.copy_to(chat_id, reply_markup=reply_markup,
                                     reply_parameters=reply_params, **extra)
    except TelegramAPIError as e:
        log.warning("Не удалось доставить сообщение в %s: %s", chat_id, e)
        return False

    add_link((msg.chat.id, msg.message_id), (chat_id, sent.message_id), both=both_ways)
    return True


async def notify(tg_id: int, text: str) -> None:
    try:
        await bot.send_message(tg_id, text)
    except TelegramAPIError as e:
        log.warning("Не удалось уведомить %s: %s", tg_id, e)


# ===== Команды =====

@dp.message(Command("start"))
async def start_cmd(msg: Message):
    cid = get_or_create_cid(msg.from_user.id)
    data["clients"][cid]["username"] = msg.from_user.username
    save_data()
    await msg.answer("Привет! Ты можешь написать сюда любое сообщение "
                     "(текст, фото, видео, голосовое…), и администратор ответит тебе анонимно.")


@dp.message(Command("info"))
async def info_cmd(msg: Message):
    if msg.from_user.id not in HIGH_ADMINS:
        return
    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("Использование: /info Номер_Пользователя")
        return
    client = data["clients"].get(parts[1])
    if client is None:
        await msg.answer("Такого пользователя нет.")
        return
    await msg.answer(f"✅ Информация на пользователя #{parts[1]}:\n\n"
                     f"{json.dumps(client, indent=2, ensure_ascii=False)}")


@dp.message(Command("untake"))
async def untake_cmd(msg: Message):
    """Открепить пользователя от админа."""
    if not is_admin(msg.from_user.id):
        return
    admin_cid = get_or_create_cid(msg.from_user.id)
    user_cid = data["clients"][admin_cid]["user"]
    if not user_cid:
        await msg.answer("У вас нет закреплённого пользователя.")
        return
    data["clients"][user_cid]["admin"] = None
    data["clients"][admin_cid]["user"] = None
    save_data()
    await msg.answer(f"✅ Пользователь #{user_cid} откреплён")


@dp.callback_query(F.data.startswith("take:"))
async def take_callback(callback: CallbackQuery):
    """Кнопка «Взять» под новым сообщением."""
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.answer("Вы не админ", show_alert=True)
        return

    cid = callback.data.split(":", 1)[1]
    client = data["clients"].get(cid)
    if client is None:
        await callback.answer("Такого пользователя нет", show_alert=True)
        return
    if client["admin"] is not None:
        await callback.answer("⚠ Этого пользователя уже взял админ", show_alert=True)
        return

    admin_cid = get_or_create_cid(admin_id)
    if data["clients"][admin_cid]["user"]:
        await callback.answer("Сначала открепите текущего: /untake", show_alert=True)
        return

    client["admin"] = admin_id
    data["clients"][admin_cid]["user"] = cid
    save_data()
    await callback.answer()
    await callback.message.answer(f"Вы взяли пользователя #{cid}")


@dp.message(Command("addadmin"))
async def add_admin_cmd(msg: Message):
    if msg.from_user.id not in HIGH_ADMINS:
        return
    target = parse_id_arg(msg)
    if target is None:
        await msg.answer("Использование: /addadmin ID (ID — число)")
        return
    if is_admin(target):
        await msg.answer("⚠️ Этот пользователь уже админ")
        return
    data["admins"].append(target)
    save_data()
    await msg.answer(f"✅ Админ добавлен: {target}")
    await notify(target, "✨ Вы стали админом! Поздравляем")


@dp.message(Command("deladmin"))
async def del_admin_cmd(msg: Message):
    if msg.from_user.id not in HIGH_ADMINS:
        return
    target = parse_id_arg(msg)
    if target is None:
        await msg.answer("Использование: /deladmin ID (ID — число)")
        return
    if not is_admin(target):
        await msg.answer(f"❌ Этот ID не является админом: {target}")
        return
    data["admins"].remove(target)
    save_data()
    await msg.answer(f"✅ Админ удалён: {target}")
    await notify(target, "🥀 Вы больше не админ")


@dp.message(Command("reply"))
async def reply_cmd(msg: Message):
    """Ответить пользователю, не беря диалог: /reply ID текст"""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer("Использование: /reply ID текст")
        return
    cid, text = parts[1], parts[2]
    client = data["clients"].get(cid)
    if client is None:
        await msg.answer("Такого пользователя нет.")
        return
    await notify(client["tg_id"], f"Админ: {text}")
    await msg.answer("Отправлено пользователю.")


# ===== Все остальные сообщения (текст, фото, видео, файлы и т.д.) =====
# Регистрируется последним; команды (начинаются с "/") сюда не попадают.

@dp.message(F.chat.type == "private", lambda m: not (m.text or "").startswith("/"))
async def any_message(msg: Message):
    sender_id = msg.from_user.id
    cid = get_or_create_cid(sender_id)
    client = data["clients"][cid]

    # --- пишет админ: пересылаем его закреплённому пользователю ---
    if is_admin(sender_id):
        target_cid = client["user"]
        if not target_cid:
            await msg.answer("У вас нет закреплённого пользователя. "
                             "Нажмите «Взять» под сообщением или используйте /reply ID текст")
            return
        await relay(msg, data["clients"][target_cid]["tg_id"], prefix="💬 Админ:\n")
        return

    # --- пишет пользователь, у которого уже есть админ ---
    if client["admin"]:
        await relay(msg, client["admin"], prefix=f"👤 Пользователь #{cid}:\n")
        return

    # --- пишет пользователь без админа: уведомляем всех админов ---
    prefix = f"⭐ Новое сообщение от #{cid}\n\n"
    keyboard = take_keyboard(cid)
    results = [await relay(msg, admin_id, prefix, keyboard, both_ways=False)
               for admin_id in data["admins"]]

    if any(results):
        await msg.answer("Твоё сообщение отправлено! ✅ Админ скоро ответит! 😊")
    else:
        await msg.answer("Не получилось доставить сообщение, попробуй чуть позже.")


# ===== Запуск =====

if __name__ == "__main__":
    print("🤖 бот живой")
    dp.run_polling(bot)
