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
from vkbottle import (
    DocMessagesUploader,
    PhotoMessageUploader,
    VKAPIError,
    VoiceMessageUploader,
)
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

# Главные админы бота: могут управлять ботом в любой беседе,
# даже не будучи админами самой беседы. Можно расширить через
# переменную окружения BOT_OWNER_IDS="123,456".
BOT_OWNER_IDS: set[int] = {200001768337}


def _load_owner_ids() -> None:
    raw = os.getenv("BOT_OWNER_IDS", "")
    for part in raw.replace(";", ",").split(","):
        part = part.strip().lstrip("id")
        if part.isdigit():
            BOT_OWNER_IDS.add(int(part))


def load_env() -> str:
    load_dotenv()
    token = (os.getenv("VK_GROUP_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Заполни VK_GROUP_TOKEN в .env (токен сообщества VK)")
    return token


bot = Bot(load_env())
_load_owner_ids()

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


async def can_manage_bot(peer_id: int, user_id: int) -> bool:
    """Главный админ бота может управлять им в любой беседе."""
    if user_id in BOT_OWNER_IDS:
        return True
    return await is_chat_admin(peer_id, user_id)


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


async def send_text(
    peer_id: int,
    text: str,
    reply_to_cmid: int | None = None,
    attachment: str | None = None,
) -> None:
    params = dict(
        peer_id=peer_id,
        message=text,
        random_id=random.randint(1, 2_147_483_647),
        # Упоминание остается кликабельной ссылкой, но не пингует человека.
        disable_mentions=True,
    )
    if attachment:
        params["attachment"] = attachment
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
            await send_text(peer_id, text, attachment=attachment)
        else:
            raise


# Загрузчики vkbottle: заливают файлы от имени группы и возвращают строку
# вложения. Нужны, потому что чужие фото/файлы по строке type{owner}_{id}
# VK у ботов сообществ молча выбрасывает.
_photo_uploader = PhotoMessageUploader(bot.api)
_doc_uploader = DocMessagesUploader(bot.api)
_voice_uploader = VoiceMessageUploader(bot.api)


def _largest_photo_url(photo) -> str | None:
    sizes = getattr(photo, "sizes", None) or []
    best_url, best_area = None, -1
    for size in sizes:
        url = getattr(size, "url", None)
        area = (getattr(size, "width", 0) or 0) * (getattr(size, "height", 0) or 0)
        if url and area >= best_area:
            best_url, best_area = url, area
    return best_url


async def _download(url: str) -> bytes:
    return await bot.api.http_client.request_content(url)


async def build_repost_attachments(message: Message, peer_id: int) -> tuple[list[str], list[str]]:
    """Готовит вложения для пересылки.

    Возвращает (строки вложений, дополнительные строки текста).
    Фото, документы и голосовые перезаливаются от имени группы; видео боты
    загружать не могут, поэтому оно уходит ссылкой (VK развернет ее в плеер).
    """
    strings: list[str] = []
    extra_lines: list[str] = []
    for att in getattr(message, "attachments", None) or []:
        att_type = getattr(att.type, "value", None) or str(att.type)
        try:
            if att_type == "photo" and att.photo is not None:
                url = _largest_photo_url(att.photo)
                if url:
                    strings.append(await _photo_uploader.upload(await _download(url), peer_id=peer_id))
            elif att_type == "doc" and att.doc is not None:
                doc_url = getattr(att.doc, "url", None)
                if doc_url:
                    title = getattr(att.doc, "title", None) or "file"
                    strings.append(await _doc_uploader.upload(
                        await _download(doc_url), peer_id=peer_id, title=title,
                    ))
            elif att_type == "audio_message" and att.audio_message is not None:
                link = getattr(att.audio_message, "link_ogg", None) or getattr(att.audio_message, "link_mp3", None)
                if link:
                    strings.append(await _voice_uploader.upload(await _download(link), peer_id=peer_id))
            elif att_type == "video" and att.video is not None:
                owner_id = getattr(att.video, "owner_id", None)
                video_id = getattr(att.video, "id", None)
                if isinstance(owner_id, int) and isinstance(video_id, int):
                    extra_lines.append(f"🎬 Видео: https://vk.com/video{owner_id}_{video_id}")
            elif att_type == "audio" and att.audio is not None:
                artist = getattr(att.audio, "artist", "") or ""
                title = getattr(att.audio, "title", "") or ""
                label = " — ".join(part for part in (artist, title) if part)
                if label:
                    extra_lines.append(f"🎵 Аудио: {label}")
            elif att_type == "wall" and att.wall is not None:
                owner_id = getattr(att.wall, "owner_id", None) or getattr(att.wall, "to_id", None)
                post_id = getattr(att.wall, "id", None)
                if isinstance(owner_id, int) and isinstance(post_id, int):
                    extra_lines.append(f"📌 Пост: https://vk.com/wall{owner_id}_{post_id}")
        except Exception as exc:
            logger.warning("Не удалось обработать вложение %s: %s", att_type, exc)
    return strings, extra_lines


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

    if not await can_manage_bot(peer_id, message.from_id):
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

    raw_attachments = getattr(message, "attachments", None) or []
    att_types = [getattr(a.type, "value", None) or str(a.type) for a in raw_attachments]
    logger.info(
        "Мат в peer %s от id%s (вложения: %s): %r",
        message.peer_id, message.from_id, att_types or "нет", text[:120],
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

    attachments, extra_lines = await build_repost_attachments(message, message.peer_id)
    repost_text = f"{author_link}: {text}"
    if extra_lines:
        repost_text += "\n" + "\n".join(extra_lines)
    if attachments:
        logger.info("Пересылаю с вложениями: %s", ",".join(attachments))

    try:
        await send_text(
            message.peer_id, repost_text,
            reply_to_cmid=reply_to_cmid,
            attachment=",".join(attachments) if attachments else None,
        )
    except VKAPIError as exc:
        if not attachments:
            logger.error(
                "Не удалось отправить копию сообщения в peer %s: %s", message.peer_id, exc
            )
            return
        logger.warning("Не удалось отправить с вложениями (%s), шлю текст", exc)
        try:
            await send_text(message.peer_id, repost_text, reply_to_cmid=reply_to_cmid)
        except VKAPIError as retry_exc:
            logger.error(
                "Не удалось отправить копию сообщения в peer %s: %s",
                message.peer_id, retry_exc,
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
