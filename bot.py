"""VK-бот для личной страницы: автомодерация мата в беседах.

Логика:
1. Слушает сообщения из бесед через User Long Poll (асинхронно, vkbottle).
2. Обычные сообщения не трогает.
3. Если в сообщении найден мат:
   - мгновенно удаляет оригинал для всех (delete_for_all=1);
   - отправляет в чат копию в формате "Имя Фамилия: <текст с цензурой>".

Требования:
- токен пользователя VK (VK_USER_TOKEN в .env);
- чтобы удалять чужие сообщения, аккаунт должен быть админом беседы.
"""

from __future__ import annotations

import logging
import os
import random
import time

from dotenv import load_dotenv
from vkbottle import VKAPIError
from vkbottle.user import Message, User

from profanity_filter import censor_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("vk-mat-filter")
logging.getLogger("vkbottle").setLevel(logging.WARNING)

CHAT_PEER_ID_START = 2_000_000_000

# Коды ошибок VK, означающие, что удалить сообщение нельзя (обычно нет прав).
DELETE_PERMISSION_ERROR_CODES = {15, 924, 925}

# Не чаще одного предупреждения о правах на беседу в этот интервал.
RIGHTS_WARNING_COOLDOWN_SEC = 300.0


def load_env() -> str:
    load_dotenv()
    token = (os.getenv("VK_USER_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Заполни VK_USER_TOKEN в .env (токен пользователя VK)")
    return token


user = User(load_env())

# Кэш имен пользователей, чтобы не дергать users.get на каждое сообщение.
_user_names: dict[int, str] = {}
# Когда мы в последний раз жаловались на отсутствие прав в конкретной беседе.
_last_rights_warning: dict[int, float] = {}
# Тексты, которые бот сам недавно отправил (защита от самосрабатывания).
_recent_own_texts: dict[str, float] = {}
RECENT_OWN_TTL_SEC = 30.0


def _remember_own_text(text: str) -> None:
    now = time.monotonic()
    _recent_own_texts[text] = now
    for key, ts in list(_recent_own_texts.items()):
        if now - ts > RECENT_OWN_TTL_SEC:
            del _recent_own_texts[key]


def _is_own_recent_text(text: str) -> bool:
    ts = _recent_own_texts.get(text)
    return ts is not None and time.monotonic() - ts <= RECENT_OWN_TTL_SEC


async def get_user_name(user_id: int) -> str:
    if user_id in _user_names:
        return _user_names[user_id]
    name = f"id{user_id}"
    try:
        if user_id > 0:
            users = await user.api.users.get(user_ids=[user_id])
            if users:
                name = f"{users[0].first_name} {users[0].last_name}".strip() or name
        else:
            groups = await user.api.groups.get_by_id(group_id=abs(user_id))
            group_list = getattr(groups, "groups", None) or groups
            if group_list:
                name = getattr(group_list[0], "name", None) or name
    except Exception as exc:
        logger.warning("Не удалось получить имя id%s: %s", user_id, exc)
    _user_names[user_id] = name
    return name


async def send_text(peer_id: int, text: str) -> None:
    _remember_own_text(text)
    await user.api.messages.send(
        peer_id=peer_id,
        message=text,
        random_id=random.randint(1, 2_147_483_647),
    )


async def delete_message(message: Message) -> bool:
    """Удаляет сообщение для всех. Возвращает True при успехе."""
    try:
        await user.api.messages.delete(
            message_ids=[message.id],
            delete_for_all=True,
        )
        return True
    except VKAPIError as exc:
        code = getattr(exc, "code", None)
        if code in DELETE_PERMISSION_ERROR_CODES:
            logger.warning(
                "Нет прав удалить сообщение %s в peer %s (ошибка VK %s)",
                message.id, message.peer_id, code,
            )
            await warn_about_rights(message.peer_id)
        else:
            logger.error(
                "Ошибка VK при удалении сообщения %s: [%s] %s",
                message.id, code, exc,
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
            "чтобы удалить сообщение.",
        )
    except VKAPIError as exc:
        logger.error("Не удалось отправить предупреждение в peer %s: %s", peer_id, exc)


@user.on.message()
async def moderate_message(message: Message) -> None:
    # Работаем только в беседах.
    if message.peer_id < CHAT_PEER_ID_START:
        return

    text = message.text or ""
    if not text.strip():
        return

    # Не реагируем на собственные служебные сообщения бота.
    if message.out and _is_own_recent_text(text):
        return

    has_profanity, censored = censor_text(text)
    if not has_profanity:
        # Обычное сообщение — не трогаем.
        return

    logger.info(
        "Мат в peer %s от id%s: %r", message.peer_id, message.from_id, text[:120]
    )

    deleted = await delete_message(message)
    if not deleted:
        return

    author_name = await get_user_name(message.from_id)
    try:
        await send_text(message.peer_id, f"{author_name}: {censored}")
    except VKAPIError as exc:
        logger.error(
            "Не удалось отправить цензурную копию в peer %s: %s", message.peer_id, exc
        )


def main() -> None:
    logger.info("Бот-фильтр мата запущен и слушает сообщения...")
    user.run()


if __name__ == "__main__":
    main()
