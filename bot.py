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
    Callback,
    DocMessagesUploader,
    GroupEventType,
    GroupTypes,
    Keyboard,
    KeyboardButtonColor,
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

# Данные храним в папке data/: на хостингах (например, Bothost) именно она
# переживает перезапуски и переустановки контейнера. Папку можно переопределить
# через BOT_DATA_DIR (тесты используют временную).
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("BOT_DATA_DIR") or os.path.join(_BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Миграция со старой схемы: раньше файлы лежали рядом с bot.py.
for _fname in ("nicknames.json", "custom_words.json", "stats.json", "mutes.json"):
    _old_path = os.path.join(_BASE_DIR, _fname)
    _new_path = os.path.join(DATA_DIR, _fname)
    if _old_path != _new_path and os.path.exists(_old_path) and not os.path.exists(_new_path):
        try:
            os.replace(_old_path, _new_path)
            logger.info("Перенес %s в %s", _fname, DATA_DIR)
        except OSError as exc:
            logger.warning("Не удалось перенести %s в %s: %s", _fname, DATA_DIR, exc)


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


# --- Статистика участников (анкета «Профиль») ------------------------------

STATS_PATH = os.path.join(DATA_DIR, "stats.json")
# Чтобы не писать на диск на каждое сообщение, сохраняем не чаще раза в N сек.
STATS_SAVE_INTERVAL_SEC = 30.0


def _load_stats() -> dict[str, dict]:
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s", STATS_PATH, exc)
        return {}


_stats: dict[str, dict] = _load_stats()
_stats_last_save = 0.0


def _save_stats(force: bool = False) -> None:
    global _stats_last_save
    now = time.monotonic()
    if not force and now - _stats_last_save < STATS_SAVE_INTERVAL_SEC:
        return
    _stats_last_save = now
    try:
        _save_json(STATS_PATH, _stats)
    except Exception as exc:
        logger.error("Не удалось сохранить %s: %s", STATS_PATH, exc)


def record_message_stat(peer_id: int, user_id: int) -> None:
    entry = _stats.setdefault(
        _nick_key(peer_id, user_id), {"msgs": 0, "viol": 0, "first": time.time()}
    )
    entry["msgs"] = entry.get("msgs", 0) + 1
    _save_stats()


def record_violation_stat(peer_id: int, user_id: int) -> None:
    entry = _stats.setdefault(
        _nick_key(peer_id, user_id), {"msgs": 0, "viol": 0, "first": time.time()}
    )
    entry["viol"] = entry.get("viol", 0) + 1
    _save_stats(force=True)


# --- Монеты (рулетка, бонус) -------------------------------------------------

COINS_START = 500
BONUS_AMOUNT = 200
BONUS_COOLDOWN_SEC = 24 * 3600


def get_coins(peer_id: int, user_id: int) -> int:
    entry = _stats.get(_nick_key(peer_id, user_id)) or {}
    return entry.get("coins", COINS_START)


def change_coins(peer_id: int, user_id: int, delta: int) -> int:
    entry = _stats.setdefault(
        _nick_key(peer_id, user_id), {"msgs": 0, "viol": 0, "first": time.time()}
    )
    entry["coins"] = max(0, entry.get("coins", COINS_START) + delta)
    _save_stats(force=True)
    return entry["coins"]


def try_claim_bonus(peer_id: int, user_id: int) -> float:
    """Начисляет ежедневный бонус. Возвращает 0 при успехе, иначе секунды до следующего."""
    entry = _stats.setdefault(
        _nick_key(peer_id, user_id), {"msgs": 0, "viol": 0, "first": time.time()}
    )
    now = time.time()
    last = entry.get("bonus_ts", 0)
    remaining = BONUS_COOLDOWN_SEC - (now - last)
    if remaining > 0:
        return remaining
    entry["bonus_ts"] = now
    entry["coins"] = entry.get("coins", COINS_START) + BONUS_AMOUNT
    _save_stats(force=True)
    return 0


# --- Предупреждения (!варн) -------------------------------------------------

WARN_LIMIT = 3


def get_warns(peer_id: int, user_id: int) -> int:
    entry = _stats.get(_nick_key(peer_id, user_id)) or {}
    return entry.get("warns", 0)


def change_warns(peer_id: int, user_id: int, delta: int) -> int:
    entry = _stats.setdefault(
        _nick_key(peer_id, user_id), {"msgs": 0, "viol": 0, "first": time.time()}
    )
    entry["warns"] = max(0, entry.get("warns", 0) + delta)
    _save_stats(force=True)
    return entry["warns"]


def reset_warns(peer_id: int, user_id: int) -> None:
    entry = _stats.get(_nick_key(peer_id, user_id))
    if entry is not None:
        entry["warns"] = 0
        _save_stats(force=True)


# --- Мут (!мут / !размут) ----------------------------------------------------

MUTES_PATH = os.path.join(DATA_DIR, "mutes.json")
MUTE_DEFAULT_SEC = 10 * 60
MUTE_MAX_SEC = 7 * 24 * 3600


def _load_mutes() -> dict[str, float]:
    try:
        with open(MUTES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s", MUTES_PATH, exc)
        return {}


_mutes: dict[str, float] = _load_mutes()


def _save_mutes() -> None:
    try:
        _save_json(MUTES_PATH, _mutes)
    except Exception as exc:
        logger.error("Не удалось сохранить %s: %s", MUTES_PATH, exc)


def is_muted(peer_id: int, user_id: int) -> bool:
    key = _nick_key(peer_id, user_id)
    until = _mutes.get(key)
    if until is None:
        return False
    if until > time.time():
        return True
    del _mutes[key]
    _save_mutes()
    return False


def set_mute(peer_id: int, user_id: int, duration_sec: int) -> float:
    until = time.time() + duration_sec
    _mutes[_nick_key(peer_id, user_id)] = until
    _save_mutes()
    return until


def clear_mute(peer_id: int, user_id: int) -> bool:
    if _mutes.pop(_nick_key(peer_id, user_id), None) is not None:
        _save_mutes()
        return True
    return False


def parse_duration_sec(raw: str) -> int | None:
    """«10м», «2ч», «30с», «1д», просто «10» (минуты)."""
    m = re.fullmatch(r"(\d+)\s*(с|сек|c|s|м|мин|m|min|ч|час|h|д|дн|d)?\.?", raw.strip().lower())
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2) or "м"
    if unit in {"с", "сек", "c", "s"}:
        mult = 1
    elif unit in {"м", "мин", "m", "min"}:
        mult = 60
    elif unit in {"ч", "час", "h"}:
        mult = 3600
    else:
        mult = 86400
    return max(1, min(value * mult, MUTE_MAX_SEC))


def format_duration(sec: float) -> str:
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400} дн"
    if sec >= 3600:
        return f"{sec // 3600} ч"
    if sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} сек"


# --- Рулетка (!рулетка) --------------------------------------------------------

ROULETTE_TTL_SEC = 10 * 60
ROULETTE_ZERO_MULT = 14
# Красные номера настоящей европейской рулетки.
ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
_roulette_bets: dict[str, dict] = {}


def _roulette_cleanup() -> None:
    now = time.monotonic()
    for gid in [g for g, bet in _roulette_bets.items() if now - bet["created"] > ROULETTE_TTL_SEC]:
        del _roulette_bets[gid]


def _roulette_keyboard(gid: str) -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("🔴 Красное x2", {"rl": "spin", "g": gid, "ch": "red"}), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Callback("⚫ Черное x2", {"rl": "spin", "g": gid, "ch": "black"}), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Callback(f"🟢 Зеро x{ROULETTE_ZERO_MULT}", {"rl": "spin", "g": gid, "ch": "zero"}), color=KeyboardButtonColor.POSITIVE)
    return kb.get_json()


# --- Крестики-нолики (!км) ----------------------------------------------------

TTT_TTL_SEC = 30 * 60
TTT_WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]
# Игры живут в памяти: {game_id: состояние}. Старые чистятся лениво.
_ttt_games: dict[str, dict] = {}


def _ttt_cleanup() -> None:
    now = time.monotonic()
    for gid in [g for g, game in _ttt_games.items() if now - game["created"] > TTT_TTL_SEC]:
        del _ttt_games[gid]


def _ttt_new_game(peer_id: int, x_id: int, target_id: int | None) -> str:
    _ttt_cleanup()
    gid = f"{random.randrange(16 ** 8):08x}"
    _ttt_games[gid] = {
        "peer": peer_id,
        "x": x_id,
        "o": None,
        "target": target_id,
        "board": [""] * 9,
        "turn": "x",
        "created": time.monotonic(),
        "finished": False,
    }
    return gid


def _ttt_winner(board: list[str]) -> str | None:
    for a, b, c in TTT_WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def _ttt_join_keyboard(gid: str) -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("⚔️ Принять вызов", {"km": "join", "g": gid}), color=KeyboardButtonColor.POSITIVE)
    return kb.get_json()


def _ttt_board_keyboard(gid: str, game: dict) -> str:
    kb = Keyboard(inline=True)
    for row in range(3):
        if row:
            kb.row()
        for col in range(3):
            i = row * 3 + col
            mark = game["board"][i]
            if mark == "x":
                label, color = "❌", KeyboardButtonColor.NEGATIVE
            elif mark == "o":
                label, color = "⭕", KeyboardButtonColor.PRIMARY
            else:
                label, color = "·", KeyboardButtonColor.SECONDARY
            kb.add(Callback(label, {"km": "move", "g": gid, "c": i}), color=color)
    return kb.get_json()


async def _ttt_text(game: dict, status_line: str) -> str:
    x_link = await _target_link(game["peer"], game["x"])
    o_link = await _target_link(game["peer"], game["o"]) if game["o"] else "?"
    return f"⭕❌ Крестики-нолики\n❌ {x_link} vs ⭕ {o_link}\n{status_line}"


async def _send_with_keyboard(peer_id: int, text: str, keyboard: str) -> int | None:
    """Шлет сообщение с клавиатурой и возвращает его conversation_message_id.

    Используем messages.send с peer_ids: только в этом варианте VK возвращает
    conversation_message_id, который нужен, чтобы потом удалить сообщение.
    """
    try:
        resp = await bot.api.request("messages.send", {
            "peer_ids": peer_id,
            "message": text,
            "random_id": random.randint(1, 2_147_483_647),
            "keyboard": keyboard,
            "disable_mentions": 1,
        })
    except VKAPIError as exc:
        logger.error("Не удалось отправить сообщение игры в peer %s: %s", peer_id, exc)
        return None
    items = resp.get("response") or []
    if isinstance(items, list) and items:
        return items[0].get("conversation_message_id")
    return None


async def _ttt_send_board(game: dict, gid: str, text: str, keyboard: str) -> None:
    """Удаляет старое сообщение игры и шлет новое, чтобы поле всегда было внизу чата."""
    peer_id = game["peer"]
    old_cmid = game.get("cmid")
    if old_cmid:
        try:
            await bot.api.messages.delete(
                peer_id=peer_id, cmids=[old_cmid], delete_for_all=True
            )
        except VKAPIError as exc:
            logger.debug("Не удалось удалить старое поле игры: %s", exc)
    game["cmid"] = await _send_with_keyboard(peer_id, text, keyboard)


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


async def resolve_user_id(raw: str) -> int | None:
    """Достает id пользователя из упоминания или ссылки.

    Понимает: [id123|Имя], vk.com/id123, vk.ru/id123, @id123, id123,
    а также короткие имена (vk.com/durov, @durov) через resolveScreenName.
    """
    raw = raw.strip()
    if not raw:
        return None
    m = re.search(r"\[id(\d+)\|", raw)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:vk\.(?:com|ru)/|@|^)id(\d+)$", raw)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:vk\.(?:com|ru)/|@)([A-Za-z0-9_.]+)", raw)
    screen_name = m.group(1) if m else (raw if re.fullmatch(r"[A-Za-z0-9_.]+", raw) else None)
    if screen_name:
        try:
            resp = await bot.api.utils.resolve_screen_name(screen_name=screen_name)
            obj_type = getattr(getattr(resp, "type", None), "value", None) or str(getattr(resp, "type", ""))
            if obj_type == "user":
                return getattr(resp, "object_id", None)
        except Exception as exc:
            logger.warning("Не удалось определить пользователя %r: %s", raw, exc)
    return None


async def handle_profile_command(message: Message, target_id: int | None = None) -> None:
    """«Кто я» / «Профиль» — анкета участника беседы.

    Если target_id не задан: команда, отправленная реплеем на чужое
    сообщение, показывает анкету того пользователя, иначе — свою.
    """
    peer_id = message.peer_id
    if target_id is None:
        target_id = message.from_id
        replied = getattr(message, "reply_message", None)
        if replied is not None and getattr(replied, "from_id", 0) > 0:
            target_id = replied.from_id

    name = await get_user_name(target_id)
    nick = get_nickname(peer_id, target_id)

    if target_id in BOT_OWNER_IDS:
        role = "👑 Главный админ бота"
    elif await is_chat_admin(peer_id, target_id):
        role = "⭐ Админ беседы"
    else:
        role = "👤 Участник"

    entry = _stats.get(_nick_key(peer_id, target_id)) or {}
    msgs = entry.get("msgs", 0)
    viol = entry.get("viol", 0)
    warns = entry.get("warns", 0)
    first = entry.get("first")
    since = time.strftime("%d.%m.%Y", time.localtime(first)) if first else "—"

    lines = [
        "📋 Анкета участника",
        "➖➖➖➖➖➖➖➖➖➖",
        f"👤 Имя: [id{target_id}|{name}]",
    ]
    if nick:
        lines.append(f"✏️ Ник в беседе: {nick}")
    lines += [
        f"🆔 Айди: id{target_id}",
        f"🎖 Роль: {role}",
        f"💬 Сообщений: {msgs}",
        f"💰 Монет: {entry.get('coins', COINS_START)}",
        f"🤬 Удалено за мат: {viol}",
        f"⚠️ Предупреждения: {warns}/{WARN_LIMIT}",
        f"📅 Первое сообщение: {since}",
    ]
    mute_until = _mutes.get(_nick_key(peer_id, target_id))
    if mute_until and mute_until > time.time():
        lines.append(f"🔇 В муте еще {format_duration(mute_until - time.time())}")
    await send_text(peer_id, "\n".join(lines))


async def extract_target(message: Message, arg: str) -> tuple[int | None, str]:
    """Определяет пользователя-цель команды: реплей или ссылка в аргументе.

    Возвращает (user_id | None, аргумент без ссылки).
    """
    replied = getattr(message, "reply_message", None)
    if replied is not None and getattr(replied, "from_id", 0) > 0:
        return replied.from_id, arg

    tokens = arg.split()
    for i, token in enumerate(tokens):
        if not re.search(r"\[id\d+|vk\.(com|ru)/|@|^id\d+$", token):
            continue
        user_id = await resolve_user_id(token)
        if user_id:
            rest = " ".join(tokens[:i] + tokens[i + 1:])
            return user_id, rest
    return None, arg


async def kick_user(peer_id: int, user_id: int) -> bool:
    try:
        await bot.api.messages.remove_chat_user(
            chat_id=peer_id - CHAT_PEER_ID_START, member_id=user_id
        )
        return True
    except VKAPIError as exc:
        logger.warning("Не удалось кикнуть id%s из peer %s: %s", user_id, peer_id, exc)
        return False


async def _target_link(peer_id: int, user_id: int) -> str:
    name = get_nickname(peer_id, user_id) or await get_user_name(user_id)
    return f"[id{user_id}|{name}]"


async def handle_warn_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    if not await can_manage_bot(peer_id, message.from_id):
        await send_text(peer_id, "⛔ Выдавать предупреждения может только админ.")
        return

    arg = raw_arg.strip()
    removing = False
    lowered = arg.lower()
    for prefix in ("снять", "убрать", "-"):
        if lowered.startswith(prefix):
            removing = True
            arg = arg[len(prefix):].strip()
            break

    target_id, _ = await extract_target(message, arg)
    if target_id is None:
        await send_text(
            peer_id,
            "⚠️ Кому предупреждение? Ответь «!варн» реплеем на сообщение "
            "или добавь ссылку: !варн vk.com/id123\n"
            "Снять: !варн снять (реплеем/ссылкой)",
        )
        return

    link = await _target_link(peer_id, target_id)

    if removing:
        if get_warns(peer_id, target_id) == 0:
            await send_text(peer_id, f"ℹ️ У {link} нет предупреждений.")
            return
        warns = change_warns(peer_id, target_id, -1)
        await send_text(peer_id, f"✅ Снял предупреждение. Теперь у {link}: {warns}/{WARN_LIMIT}")
        return

    if target_id in BOT_OWNER_IDS:
        await send_text(peer_id, "😎 Главному админу бота предупреждения не выдаются.")
        return

    warns = change_warns(peer_id, target_id, +1)
    if warns < WARN_LIMIT:
        await send_text(peer_id, f"⚠️ {link} получает предупреждение: {warns}/{WARN_LIMIT}")
        return

    if await kick_user(peer_id, target_id):
        reset_warns(peer_id, target_id)
        await send_text(peer_id, f"🚫 {link} набрал {WARN_LIMIT}/{WARN_LIMIT} предупреждений и исключен из беседы.")
    else:
        await send_text(
            peer_id,
            f"⚠️ {link} набрал {WARN_LIMIT}/{WARN_LIMIT}, но у меня нет прав исключить его. "
            "Дайте боту права администратора беседы.",
        )


async def handle_mute_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    if not await can_manage_bot(peer_id, message.from_id):
        await send_text(peer_id, "⛔ Выдавать мут может только админ.")
        return

    target_id, rest = await extract_target(message, raw_arg.strip())
    if target_id is None:
        await send_text(
            peer_id,
            "🔇 Кого замутить? Ответь «!мут 10м» реплеем на сообщение "
            "или добавь ссылку: !мут vk.com/id123 30м\n"
            "Время: 30с, 10м, 2ч, 1д (по умолчанию 10 минут).",
        )
        return

    if target_id in BOT_OWNER_IDS:
        await send_text(peer_id, "😎 Главного админа бота замутить нельзя.")
        return

    duration = MUTE_DEFAULT_SEC
    for token in rest.split():
        parsed = parse_duration_sec(token)
        if parsed is not None:
            duration = parsed
            break

    set_mute(peer_id, target_id, duration)
    link = await _target_link(peer_id, target_id)
    await send_text(
        peer_id,
        f"🔇 {link} замучен на {format_duration(duration)}. "
        "Все его сообщения будут удаляться.\nСнять: !размут",
    )


async def handle_unmute_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    if not await can_manage_bot(peer_id, message.from_id):
        await send_text(peer_id, "⛔ Снимать мут может только админ.")
        return

    target_id, _ = await extract_target(message, raw_arg.strip())
    if target_id is None:
        await send_text(peer_id, "🔊 Кого размутить? Ответь «!размут» реплеем или добавь ссылку.")
        return

    link = await _target_link(peer_id, target_id)
    if clear_mute(peer_id, target_id):
        await send_text(peer_id, f"🔊 {link} размучен, может писать снова.")
    else:
        await send_text(peer_id, f"ℹ️ {link} и так не в муте.")


async def handle_stats_command(message: Message) -> None:
    peer_id = message.peer_id
    prefix = f"{peer_id}:"
    entries: dict[int, dict] = {}
    for key, entry in _stats.items():
        if key.startswith(prefix):
            try:
                entries[int(key[len(prefix):])] = entry
            except ValueError:
                continue

    total_msgs = sum(e.get("msgs", 0) for e in entries.values())
    total_viol = sum(e.get("viol", 0) for e in entries.values())
    total_warns = sum(e.get("warns", 0) for e in entries.values())
    now = time.time()
    muted = sum(
        1 for key, until in _mutes.items() if key.startswith(prefix) and until > now
    )

    lines = [
        "📊 Статистика беседы",
        "➖➖➖➖➖➖➖➖➖➖",
        f"💬 Сообщений: {total_msgs}",
        f"🤬 Удалено за мат: {total_viol}",
        f"⚠️ Активных предупреждений: {total_warns}",
        f"🔇 В муте: {muted}",
    ]

    top_active = sorted(entries.items(), key=lambda kv: kv[1].get("msgs", 0), reverse=True)[:5]
    top_active = [(uid, e) for uid, e in top_active if e.get("msgs", 0) > 0]
    if top_active:
        lines.append("\n🏆 Самые активные:")
        for i, (uid, e) in enumerate(top_active, 1):
            lines.append(f"{i}. {await _target_link(peer_id, uid)} — {e.get('msgs', 0)}")

    top_viol = sorted(entries.items(), key=lambda kv: kv[1].get("viol", 0), reverse=True)[:5]
    top_viol = [(uid, e) for uid, e in top_viol if e.get("viol", 0) > 0]
    if top_viol:
        lines.append("\n🤬 Топ нарушителей:")
        for i, (uid, e) in enumerate(top_viol, 1):
            lines.append(f"{i}. {await _target_link(peer_id, uid)} — {e.get('viol', 0)}")

    top_rich = sorted(
        entries.items(), key=lambda kv: kv[1].get("coins", COINS_START), reverse=True
    )[:5]
    top_rich = [(uid, e) for uid, e in top_rich if e.get("coins", COINS_START) > 0]
    if top_rich:
        lines.append("\n💰 Топ богачей:")
        for i, (uid, e) in enumerate(top_rich, 1):
            lines.append(f"{i}. {await _target_link(peer_id, uid)} — {e.get('coins', COINS_START)}")

    await send_text(peer_id, "\n".join(lines))


async def handle_roulette_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    user_id = message.from_id
    arg = raw_arg.strip().lower()
    balance = get_coins(peer_id, user_id)

    if not arg or arg in {"помощь", "help"}:
        await send_text(
            peer_id,
            "🎰 Рулетка:\n"
            "!рулетка <ставка> — сделать ставку (например: !рулетка 50)\n"
            "!рулетка все — поставить всё\n"
            f"Красное/черное — выигрыш x2, зеро — x{ROULETTE_ZERO_MULT}\n"
            f"💰 Твой баланс: {balance} монет. Пополнение: !бонус (раз в сутки)",
        )
        return

    if arg in {"все", "всё", "all", "олл"}:
        bet = balance
    elif arg.isdigit():
        bet = int(arg)
    else:
        await send_text(peer_id, "🤔 Ставка — это число: !рулетка 50 (или: !рулетка все)")
        return

    if bet <= 0:
        await send_text(peer_id, "😅 Ставка должна быть больше нуля.")
        return
    if bet > balance:
        await send_text(
            peer_id,
            f"💸 Не хватает монет: у тебя {balance}, ставка {bet}.\n"
            "Забери ежедневный бонус: !бонус",
        )
        return

    _roulette_cleanup()
    gid = f"{random.randrange(16 ** 8):08x}"
    link = await _target_link(peer_id, user_id)
    text = (
        f"🎰 {link} ставит {bet} монет!\n"
        "Выбирай, на что ставим:"
    )
    bet_state = {
        "peer": peer_id, "user": user_id, "bet": bet,
        "created": time.monotonic(), "cmid": None,
    }
    _roulette_bets[gid] = bet_state
    bet_state["cmid"] = await _send_with_keyboard(peer_id, text, _roulette_keyboard(gid))
    if bet_state["cmid"] is None:
        _roulette_bets.pop(gid, None)


async def _roulette_handle_event(obj, payload: dict) -> str | None:
    gid = payload.get("g")
    bet_state = _roulette_bets.get(gid)
    if bet_state is None:
        return "Ставка устарела, сделай новую: !рулетка <число>"
    if obj.user_id != bet_state["user"]:
        return "Это не твоя ставка 🙂"

    choice = payload.get("ch")
    if choice not in {"red", "black", "zero"}:
        return None

    peer_id = bet_state["peer"]
    user_id = bet_state["user"]
    bet = bet_state["bet"]
    _roulette_bets.pop(gid, None)

    balance = get_coins(peer_id, user_id)
    if bet > balance:
        return f"Не хватает монет: у тебя {balance}, ставка {bet}."

    roll = random.randint(0, 36)
    if roll == 0:
        color, color_name = "zero", "🟢 Зеро"
    elif roll in ROULETTE_RED:
        color, color_name = "red", "🔴 Красное"
    else:
        color, color_name = "black", "⚫ Черное"

    if choice == color:
        win_mult = ROULETTE_ZERO_MULT if choice == "zero" else 2
        delta = bet * (win_mult - 1)
        outcome = f"🎉 Выигрыш {bet * win_mult} монет (x{win_mult})!"
    else:
        delta = -bet
        outcome = f"😢 Ставка {bet} монет сгорела."

    new_balance = change_coins(peer_id, user_id, delta)

    old_cmid = bet_state.get("cmid")
    if old_cmid:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[old_cmid], delete_for_all=True)
        except VKAPIError as exc:
            logger.debug("Не удалось удалить сообщение ставки: %s", exc)

    link = await _target_link(peer_id, user_id)
    await send_text(
        peer_id,
        f"🎰 Выпало: {roll} {color_name}\n{link}: {outcome}\n💰 Баланс: {new_balance} монет",
    )
    return None


async def handle_balance_command(message: Message) -> None:
    peer_id = message.peer_id
    balance = get_coins(peer_id, message.from_id)
    link = await _target_link(peer_id, message.from_id)
    await send_text(peer_id, f"💰 {link}, у тебя {balance} монет.\nИграть: !рулетка <ставка>. Бонус: !бонус")


async def handle_bonus_command(message: Message) -> None:
    peer_id = message.peer_id
    link = await _target_link(peer_id, message.from_id)
    remaining = try_claim_bonus(peer_id, message.from_id)
    if remaining > 0:
        await send_text(
            peer_id,
            f"⏳ {link}, бонус уже забран. Следующий через {format_duration(remaining)}.",
        )
        return
    balance = get_coins(peer_id, message.from_id)
    await send_text(peer_id, f"🎁 {link} получает {BONUS_AMOUNT} монет!\n💰 Баланс: {balance}")


async def handle_ttt_command(message: Message, raw_arg: str) -> None:
    peer_id = message.peer_id
    arg = raw_arg.strip()
    lowered = arg.lower()

    if not lowered or lowered in {"помощь", "help"}:
        await send_text(
            peer_id,
            "⭕❌ Крестики-нолики:\n"
            "!км вызов — вызов всей беседе (кто первым нажмет кнопку, играет за ⭕)\n"
            "!км вызов (реплеем или со ссылкой) — вызвать конкретного игрока",
        )
        return

    if not lowered.startswith("вызов"):
        await send_text(peer_id, "🤔 Не понял. Напиши: !км вызов")
        return

    target_id, _ = await extract_target(message, arg[len("вызов"):].strip())
    if target_id == message.from_id:
        await send_text(peer_id, "🙃 Нельзя вызвать самого себя.")
        return

    gid = _ttt_new_game(peer_id, message.from_id, target_id)
    game = _ttt_games[gid]
    challenger = await _target_link(peer_id, message.from_id)
    if target_id:
        opponent = await _target_link(peer_id, target_id)
        text = (
            f"⚔️ {challenger} вызывает {opponent} сыграть в крестики-нолики!\n"
            f"Принять вызов может только {opponent}."
        )
    else:
        text = (
            f"⚔️ {challenger} вызывает беседу сыграть в крестики-нолики!\n"
            "Кто первым нажмет кнопку — играет за ⭕."
        )
    await _ttt_send_board(game, gid, text, _ttt_join_keyboard(gid))
    if game.get("cmid") is None:
        _ttt_games.pop(gid, None)


async def _ttt_handle_event(obj, payload: dict, action: str) -> str | None:
    """Обрабатывает нажатие кнопки. Возвращает текст для снекбара или None."""
    gid = payload.get("g")
    game = _ttt_games.get(gid)
    if game is None or game["finished"]:
        return "Игра уже завершена или устарела."

    user_id = obj.user_id
    peer_id = obj.peer_id

    if action == "join":
        if game["o"] is not None:
            return "Игра уже началась."
        if user_id == game["x"]:
            return "Нельзя играть с самим собой 🙂"
        if game["target"] and user_id != game["target"]:
            return "Этот вызов адресован другому игроку."
        game["o"] = user_id
        status = f"Ход: ❌ {await _target_link(peer_id, game['x'])}"
        await _ttt_send_board(game, gid, await _ttt_text(game, status), _ttt_board_keyboard(gid, game))
        return None

    if action == "move":
        if game["o"] is None:
            return "Игра еще не началась — сначала кто-то должен принять вызов."
        if user_id not in (game["x"], game["o"]):
            return "Ты не участвуешь в этой игре."
        mark = "x" if user_id == game["x"] else "o"
        if game["turn"] != mark:
            return "Сейчас не твой ход."
        cell = payload.get("c")
        if not isinstance(cell, int) or not 0 <= cell < 9 or game["board"][cell]:
            return "Эта клетка занята."

        game["board"][cell] = mark
        winner = _ttt_winner(game["board"])
        if winner == "draw":
            game["finished"] = True
            status = "🤝 Ничья!"
        elif winner:
            game["finished"] = True
            win_id = game["x"] if winner == "x" else game["o"]
            emoji = "❌" if winner == "x" else "⭕"
            status = f"🏆 Победил {emoji} {await _target_link(peer_id, win_id)}!"
        else:
            game["turn"] = "o" if mark == "x" else "x"
            next_id = game["x"] if game["turn"] == "x" else game["o"]
            emoji = "❌" if game["turn"] == "x" else "⭕"
            status = f"Ход: {emoji} {await _target_link(peer_id, next_id)}"

        await _ttt_send_board(game, gid, await _ttt_text(game, status), _ttt_board_keyboard(gid, game))
        if game["finished"]:
            _ttt_games.pop(gid, None)
        return None

    return None


@bot.on.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=GroupTypes.MessageEvent)
async def on_message_event(event: GroupTypes.MessageEvent) -> None:
    obj = event.object
    payload = obj.payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    snackbar = None
    try:
        if payload.get("km"):
            snackbar = await _ttt_handle_event(obj, payload, payload["km"])
        elif payload.get("rl"):
            snackbar = await _roulette_handle_event(obj, payload)
    except Exception as exc:
        logger.error("Ошибка обработки кнопки: %s", exc)

    event_data = None
    if snackbar:
        event_data = json.dumps({"type": "show_snackbar", "text": snackbar}, ensure_ascii=False)
    try:
        await bot.api.messages.send_message_event_answer(
            event_id=obj.event_id, user_id=obj.user_id,
            peer_id=obj.peer_id, event_data=event_data,
        )
    except VKAPIError as exc:
        logger.warning("Не удалось ответить на нажатие кнопки: %s", exc)


async def handle_admin_info_command(message: Message) -> None:
    """!н — показать, кто администратор бота, со ссылкой для связи."""
    lines = []
    for owner_id in sorted(BOT_OWNER_IDS):
        name = await get_user_name(owner_id)
        lines.append(f"👑 [id{owner_id}|{name}]")
    await send_text(
        message.peer_id,
        "Администратор бота:\n"
        + "\n".join(lines)
        + "\n\n❓ По всем вопросам о боте пишите ему в личные сообщения.",
    )


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

    # Замученные пишут «в пустоту»: удаляем всё, включая вложения без текста.
    if is_muted(message.peer_id, message.from_id):
        await delete_message(message)
        return

    text = message.text or ""
    if not text.strip():
        return

    record_message_stat(message.peer_id, message.from_id)

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
    if command in {"!н", "!n"}:
        await handle_admin_info_command(message)
        return
    if command in {"!варн", "!warn"}:
        await handle_warn_command(message, command_arg)
        return
    if command in {"!мут", "!mute"}:
        await handle_mute_command(message, command_arg)
        return
    if command in {"!размут", "!анмут", "!unmute"}:
        await handle_unmute_command(message, command_arg)
        return
    if command in {"!стата", "!статистика", "!stats"} and not command_arg:
        await handle_stats_command(message)
        return
    if command in {"!км", "!km"}:
        await handle_ttt_command(message, command_arg)
        return
    if command in {"!рулетка", "!казино", "!roulette"}:
        await handle_roulette_command(message, command_arg)
        return
    if command in {"!баланс", "!balance"} or (command == "баланс" and not command_arg):
        await handle_balance_command(message)
        return
    if command in {"!бонус", "!bonus"} or (command == "бонус" and not command_arg):
        await handle_bonus_command(message)
        return
    # «Кто я» и синонимы — анкета вызвавшего (или того, на кого ответили).
    # Срабатывает только если сообщение состоит из одной команды, чтобы
    # не реагировать на обычные фразы.
    lowered_text = " ".join(text.lower().split()).rstrip("?!.")
    if lowered_text in {
        "кто я", "хто я", "!роль", "!кто я",
        "профиль", "!профиль", "анкета", "!анкета", "profile",
    }:
        await handle_profile_command(message)
        return

    if lowered_text == "стата":
        await handle_stats_command(message)
        return

    # «Кто ты» — анкета указанного пользователя (реплей или ссылка).
    for prefix in ("кто ты", "хто ты", "!кто ты"):
        if lowered_text == prefix or lowered_text.startswith(prefix + " "):
            arg = text.strip()[len(prefix):].strip()
            target_id = None
            replied = getattr(message, "reply_message", None)
            if replied is not None and getattr(replied, "from_id", 0) > 0:
                target_id = replied.from_id
            if target_id is None and arg:
                target_id = await resolve_user_id(arg)
            if target_id is None:
                await send_text(
                    message.peer_id,
                    "🤔 Кого показать? Ответь командой «Кто ты» реплеем на сообщение "
                    "или добавь ссылку: Кто ты vk.com/id123",
                )
                return
            await handle_profile_command(message, target_id)
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

    record_violation_stat(message.peer_id, message.from_id)

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
