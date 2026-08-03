"""VK-бот сообщества: автомодерация мата в беседах.

Логика:
1. Слушает сообщения бесед через Bots Long Poll (асинхронно, vkbottle).
2. Обычные сообщения не трогает.
3. Если в сообщении найден мат:
   - мгновенно удаляет оригинал для всех (delete_for_all=1);
   - отправляет в чат копию в формате "Имя Фамилия: <оригинальный текст>".

Требования:
- токен сообщества VK (VK_GROUP_TOKEN в .env) с правом на сообщения;
- в настройках сообщества включен Long Poll API и событие "Входящее сообщение";
- бот добавлен в беседу и назначен администратором беседы
  (иначе VK не даст удалять чужие сообщения).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time

from dotenv import load_dotenv
from vkbottle import VKAPIError
from vkbottle.bot import Bot, Message

from profanity_filter import contains_profanity, text_core

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("vk-mat-filter")
logging.getLogger("vkbottle").setLevel(logging.WARNING)

CHAT_PEER_ID_START = 2_000_000_000

# Коды ошибок VK, означающие, что удалить сообщение нельзя (обычно нет прав
# администратора беседы у бота).
DELETE_PERMISSION_ERROR_CODES = {15, 917, 924, 925}

# Не чаще одного предупреждения о правах на беседу в этот интервал.
RIGHTS_WARNING_COOLDOWN_SEC = 300.0


def load_env() -> str:
    load_dotenv()
    token = (os.getenv("VK_GROUP_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Заполни VK_GROUP_TOKEN в .env (токен сообщества VK)")
    return token


bot = Bot(load_env())

# Кэш имен пользователей, чтобы не дергать users.get на каждое сообщение.
_user_names: dict[int, str] = {}
# Когда мы в последний раз жаловались на отсутствие прав в конкретной беседе.
_last_rights_warning: dict[int, float] = {}

# --- Хранилище данных бота ------------------------------------------------

# Папку с данными можно вынести (например, тесты используют временную,
# чтобы не трогать файлы живого бота).
DATA_DIR = os.getenv("BOT_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)


def _save_json(path: str, data) -> None:
    """Атомарная запись: сначала во временный файл, потом rename."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


# --- Ники ---------------------------------------------------------------

NICKNAMES_PATH = os.path.join(DATA_DIR, "nicknames.json")
NICKNAME_MAX_LEN = 48


def _load_nicknames() -> dict[str, str]:
    try:
        with open(NICKNAMES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s", NICKNAMES_PATH, exc)
        return {}


_nicknames: dict[str, str] = _load_nicknames()


def _save_nicknames() -> None:
    try:
        _save_json(NICKNAMES_PATH, _nicknames)
    except Exception as exc:
        logger.error("Не удалось сохранить %s: %s", NICKNAMES_PATH, exc)


def _nick_key(peer_id: int, user_id: int) -> str:
    return f"{peer_id}:{user_id}"


def get_nickname(peer_id: int, user_id: int) -> str | None:
    return _nicknames.get(_nick_key(peer_id, user_id))


def set_nickname(peer_id: int, user_id: int, nick: str) -> None:
    _nicknames[_nick_key(peer_id, user_id)] = nick
    _save_nicknames()


def clear_nickname(peer_id: int, user_id: int) -> bool:
    if _nicknames.pop(_nick_key(peer_id, user_id), None) is not None:
        _save_nicknames()
        return True
    return False


def sanitize_nickname(raw: str) -> str:
    # Убираем символы разметки упоминаний VK, чтобы не ломать ссылку [id|ник].
    nick = raw.replace("[", "").replace("]", "").replace("|", "")
    nick = re.sub(r"\s+", " ", nick).strip()
    return nick[:NICKNAME_MAX_LEN].strip()


# --- Свой список запрещенных слов (!мат) ----------------------------------

CUSTOM_WORDS_PATH = os.path.join(DATA_DIR, "custom_words.json")
CUSTOM_WORD_MIN_CORE_LEN = 3


def _load_custom_words() -> list[str]:
    try:
        with open(CUSTOM_WORDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [w for w in data if isinstance(w, str)] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s", CUSTOM_WORDS_PATH, exc)
        return []


_custom_words: list[str] = _load_custom_words()
# Нормализованные "ядра" слов — по ним идет поиск в сообщениях.
_custom_cores: dict[str, str] = {w: text_core(w) for w in _custom_words}


def _save_custom_words() -> None:
    try:
        _save_json(CUSTOM_WORDS_PATH, _custom_words)
    except Exception as exc:
        logger.error("Не удалось сохранить %s: %s", CUSTOM_WORDS_PATH, exc)


def add_custom_word(word: str) -> bool:
    core = text_core(word)
    if core in _custom_cores.values():
        return False
    _custom_words.append(word)
    _custom_cores[word] = core
    _save_custom_words()
    return True


def remove_custom_word(word: str) -> bool:
    core = text_core(word)
    for existing, existing_core in list(_custom_cores.items()):
        if existing_core == core:
            _custom_words.remove(existing)
            del _custom_cores[existing]
            _save_custom_words()
            return True
    return False


def matches_custom_word(text: str) -> bool:
    if not _custom_cores:
        return False
    core = text_core(text)
    return any(word_core and word_core in core for word_core in _custom_cores.values())


# --- Проверка прав администратора беседы -----------------------------------

_chat_admins_cache: dict[int, tuple[float, set[int]]] = {}
CHAT_ADMINS_CACHE_TTL_SEC = 60.0


async def is_chat_admin(peer_id: int, user_id: int) -> bool:
    now = time.monotonic()
    cached = _chat_admins_cache.get(peer_id)
    if cached and now - cached[0] < CHAT_ADMINS_CACHE_TTL_SEC:
        return user_id in cached[1]

    admins: set[int] = set()
    try:
        response = await bot.api.messages.get_conversations_by_id(peer_ids=[peer_id])
        items = getattr(response, "items", None) or []
        if items:
            settings = getattr(items[0], "chat_settings", None)
            if settings is not None:
                owner_id = getattr(settings, "owner_id", None)
                if isinstance(owner_id, int):
                    admins.add(owner_id)
                admins.update(getattr(settings, "admin_ids", None) or [])
    except Exception as exc:
        logger.warning("Не удалось получить админов peer %s: %s", peer_id, exc)

    _chat_admins_cache[peer_id] = (now, admins)
    return user_id in admins


async def get_user_name(user_id: int) -> str:
    if user_id in _user_names:
        return _user_names[user_id]
    name = f"id{user_id}"
    try:
        users = await bot.api.users.get(user_ids=[user_id])
        if users:
            name = f"{users[0].first_name} {users[0].last_name}".strip() or name
    except Exception as exc:
        logger.warning("Не удалось получить имя id%s: %s", user_id, exc)
    _user_names[user_id] = name
    return name


async def send_text(peer_id: int, text: str, reply_to_cmid: int | None = None) -> None:
    params = dict(
        peer_id=peer_id,
        message=text,
        random_id=random.randint(1, 2_147_483_647),
        # Упоминание остается кликабельной ссылкой, но не пингует человека.
        disable_mentions=True,
    )
    if reply_to_cmid is not None:
        # Реплей по conversation_message_id — штатный способ для ботов сообществ.
        params["forward"] = json.dumps({
            "peer_id": peer_id,
            "conversation_message_ids": [reply_to_cmid],
            "is_reply": True,
        })
    try:
        await bot.api.messages.send(**params)
    except VKAPIError as exc:
        # Если сообщение, на которое отвечаем, уже удалено — шлем без реплея.
        if reply_to_cmid is not None:
            logger.warning(
                "Не удалось ответить реплеем на cmid=%s (%s), отправляю без реплея",
                reply_to_cmid, exc,
            )
            await send_text(peer_id, text)
        else:
            raise


async def delete_message(message: Message) -> bool:
    """Удаляет сообщение для всех. Возвращает True при успехе.

    У ботов сообществ в беседах message.id == 0, поэтому удаляем
    по conversation_message_id (cmids) + peer_id.
    """
    try:
        await bot.api.messages.delete(
            peer_id=message.peer_id,
            cmids=[message.conversation_message_id],
            delete_for_all=True,
        )
        return True
    except VKAPIError as exc:
        code = getattr(exc, "code", None)
        if code in DELETE_PERMISSION_ERROR_CODES:
            logger.warning(
                "Нет прав удалить сообщение cmid=%s в peer %s (ошибка VK %s)",
                message.conversation_message_id, message.peer_id, code,
            )
            await warn_about_rights(message.peer_id)
        else:
            logger.error(
                "Ошибка VK при удалении сообщения cmid=%s: [%s] %s",
                message.conversation_message_id, code, exc,
            )
        return False


async def warn_about_rights(peer_id: int) -> None:
    """Пишет в беседу, что боту нужны права администратора (без спама)."""
    now = time.monotonic()
    last = _last_rights_warning.get(peer_id, 0.0)
    if now - last < RIGHTS_WARNING_COOLDOWN_SEC:
        return
    _last_rights_warning[peer_id] = now
    try:
        await send_text(
            peer_id,
            "⚠️ Обнаружен мат, но у меня нет прав администратора беседы, "
            "чтобы удалить сообщение. Назначьте бота админом.",
        )
    except VKAPIError as exc:
        logger.error("Не удалось отправить предупреждение в peer %s: %s", peer_id, exc)


async def handle_nick_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    user_id = message.from_id

    if not raw_arg.strip():
        current = get_nickname(peer_id, user_id)
        hint = f"Твой текущий ник: {current}\n" if current else ""
        await send_text(
            peer_id,
            f"{hint}✏️ Используй: !ник <новый ник>\n"
            "Сбросить: !ник сброс",
        )
        return

    if raw_arg.strip().lower() in {"сброс", "удалить", "reset"}:
        real_name = await get_user_name(user_id)
        if clear_nickname(peer_id, user_id):
            await send_text(peer_id, f"✅ Ник сброшен. Теперь ты снова {real_name}.")
        else:
            await send_text(peer_id, f"У тебя и не было ника, ты {real_name}.")
        return

    nick = sanitize_nickname(raw_arg)
    if not nick:
        await send_text(peer_id, "❌ Такой ник не подойдет, попробуй другой.")
        return

    if contains_profanity(nick):
        await send_text(peer_id, "❌ Ник с матом нельзя. Придумай другой.")
        return

    set_nickname(peer_id, user_id, nick)
    await send_text(peer_id, f"✅ Готово! Теперь ты [id{user_id}|{nick}].")


async def handle_mat_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    arg = raw_arg.strip()

    if not arg:
        await send_text(
            peer_id,
            "📝 Свой список запрещенных слов:\n"
            "!мат <слово> — добавить (только админ беседы)\n"
            "!мат удалить <слово> — убрать\n"
            "!мат список — показать все",
        )
        return

    lowered = arg.lower()
    if lowered in {"список", "лист", "list"}:
        if not _custom_words:
            await send_text(peer_id, "📭 Свой список пуст. Добавь: !мат <слово>")
            return
        lines = "\n".join(f"{i + 1}. {w}" for i, w in enumerate(_custom_words))
        await send_text(peer_id, f"📝 Свои запрещенные слова ({len(_custom_words)}):\n{lines}")
        return

    if not await is_chat_admin(peer_id, message.from_id):
        await send_text(peer_id, "⛔ Добавлять и удалять слова может только админ беседы.")
        return

    if lowered.startswith(("удалить ", "убрать ", "-")):
        word = arg.split(maxsplit=1)[1] if " " in arg else arg.lstrip("-").strip()
        if remove_custom_word(word):
            await send_text(peer_id, f"✅ Слово убрано из списка: {word}")
        else:
            await send_text(peer_id, f"❌ Слова нет в списке: {word}")
        return

    word = arg
    core = text_core(word)
    if len(core) < CUSTOM_WORD_MIN_CORE_LEN:
        await send_text(peer_id, "❌ Слишком короткое слово, нужно минимум 3 буквы.")
        return
    if add_custom_word(word):
        await send_text(peer_id, f"✅ Добавил в запрещенные: {word}\nТеперь буду удалять сообщения с ним.")
    else:
        await send_text(peer_id, f"ℹ️ Это слово уже в списке: {word}")


@bot.on.message()
async def moderate_message(message: Message) -> None:
    logger.debug(
        "Сообщение peer=%s from=%s cmid=%s: %r",
        message.peer_id, message.from_id, message.conversation_message_id,
        (message.text or "")[:80],
    )
    # Работаем только в беседах.
    if message.peer_id < CHAT_PEER_ID_START:
        return

    # Сообщения от сообществ (в том числе собственные репосты бота) не трогаем.
    if message.from_id <= 0:
        return

    text = message.text or ""
    if not text.strip():
        return

    # Команды бота.
    parts = text.split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    command_arg = parts[1] if len(parts) > 1 else ""
    if command in {"!ник", "!nick"}:
        await handle_nick_command(message, command_arg)
        return
    if command in {"!мат", "!mat"}:
        await handle_mat_command(message, command_arg)
        return

    if not contains_profanity(text) and not matches_custom_word(text):
        # Обычное сообщение — не трогаем.
        return

    logger.info(
        "Мат в peer %s от id%s: %r", message.peer_id, message.from_id, text[:120]
    )

    deleted = await delete_message(message)
    if not deleted:
        return

    # Пересылаем оригинальный текст без цензуры. Имя автора — кликабельная
    # ссылка на его профиль: [id123|Имя Фамилия] (или установленный !ник).
    # Если оригинал был реплеем — отвечаем реплеем на то же сообщение.
    author_name = get_nickname(message.peer_id, message.from_id) or await get_user_name(message.from_id)
    author_link = f"[id{message.from_id}|{author_name}]"
    reply_to_cmid = None
    replied = getattr(message, "reply_message", None)
    if replied is not None:
        cmid = getattr(replied, "conversation_message_id", None)
        if isinstance(cmid, int):
            reply_to_cmid = cmid
    try:
        await send_text(message.peer_id, f"{author_link}: {text}", reply_to_cmid=reply_to_cmid)
    except VKAPIError as exc:
        logger.error(
            "Не удалось отправить копию сообщения в peer %s: %s", message.peer_id, exc
        )


def main() -> None:
    logger.info(
        "Бот-фильтр мата запущен и слушает сообщения... "
        "(ников: %s, своих слов: %s)",
        len(_nicknames), len(_custom_words),
    )
    bot.run()


if __name__ == "__main__":
    main()
