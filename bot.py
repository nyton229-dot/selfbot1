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

import logging
import os
import random
import time

from dotenv import load_dotenv
from vkbottle import VKAPIError
from vkbottle.bot import Bot, Message

from profanity_filter import contains_profanity

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


async def send_text(peer_id: int, text: str) -> None:
    await bot.api.messages.send(
        peer_id=peer_id,
        message=text,
        random_id=random.randint(1, 2_147_483_647),
        # Упоминание остается кликабельной ссылкой, но не пингует человека.
        disable_mentions=True,
    )


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

    if not contains_profanity(text):
        # Обычное сообщение — не трогаем.
        return

    logger.info(
        "Мат в peer %s от id%s: %r", message.peer_id, message.from_id, text[:120]
    )

    deleted = await delete_message(message)
    if not deleted:
        return

    # Пересылаем оригинальный текст без цензуры. Имя автора — кликабельная
    # ссылка на его профиль: [id123|Имя Фамилия].
    author_name = await get_user_name(message.from_id)
    author_link = f"[id{message.from_id}|{author_name}]"
    try:
        await send_text(message.peer_id, f"{author_link}: {text}")
    except VKAPIError as exc:
        logger.error(
            "Не удалось отправить копию сообщения в peer %s: %s", message.peer_id, exc
        )


def main() -> None:
    logger.info("Бот-фильтр мата запущен и слушает сообщения...")
    bot.run()


if __name__ == "__main__":
    main()
