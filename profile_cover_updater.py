import asyncio
import io
import json
import os
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method/"
DEFAULT_FREE_BG_API_URL = "https://picsum.photos/1920/768"
DEFAULT_FREE_BG_FALLBACK_URLS = (
    "https://loremflickr.com/1920/768",
    "https://picsum.photos/seed/vkcover/1920/768",
)


def get_moscow_timezone():
    try:
        return ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:
        # Fallback для систем без tzdata: МСК стабильно UTC+3.
        return timezone(timedelta(hours=3), name="MSK")


MSK_TZ = get_moscow_timezone()

# Рисуем в высоком разрешении для качества.
RENDER_WIDTH = 1920
RENDER_HEIGHT = 768

# Для личной страницы VK принимает максимум 960x384.
CROP_WIDTH = 960
CROP_HEIGHT = 384
DEFAULT_COVER_FONT = "ofont.ru_Bebas Neue.ttf"

RU_MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
RU_WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def format_date_line_ru(msk_now: datetime) -> str:
    month = RU_MONTHS[msk_now.month - 1]
    weekday = RU_WEEKDAYS[msk_now.weekday()]
    return f"{msk_now.day} {month}, {weekday.capitalize()}"


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    x_center: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    stroke_fill: tuple[int, int, int, int],
    stroke_width: int,
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    # Учитываем внутренние смещения шрифта (left/top bearing),
    # чтобы текст был визуально ровным.
    x = x_center - width // 2 - box[0]
    y_draw = y - box[1]
    if stroke_width > 0:
        draw.text(
            (x, y_draw),
            text,
            fill=fill,
            font=font,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
    else:
        draw.text((x, y_draw), text, fill=fill, font=font)


def build_default_background() -> Image.Image:
    background = Image.new("RGBA", (RENDER_WIDTH, RENDER_HEIGHT), (20, 26, 66, 255))
    draw = ImageDraw.Draw(background)

    # Мягкий градиент от темно-синего к фиолетовому.
    for y in range(RENDER_HEIGHT):
        t = y / max(RENDER_HEIGHT - 1, 1)
        r = int(24 + 120 * t)
        g = int(30 + 40 * (1 - t))
        b = int(86 + 120 * (1 - t))
        draw.line([(0, y), (RENDER_WIDTH, y)], fill=(r, g, b, 255))

    # Световые "волны" для более живого вида.
    draw.ellipse((-220, -260, 980, 520), fill=(85, 180, 255, 95))
    draw.ellipse((380, -220, 1780, 600), fill=(255, 120, 235, 85))
    draw.ellipse((900, -320, 2200, 560), fill=(120, 110, 255, 75))
    draw.ellipse((620, 220, 1900, 1020), fill=(40, 30, 120, 145))
    return background


def normalize_background_image(source: Image.Image) -> Image.Image:
    source = source.convert("RGBA")
    # Центр-кроп до целевого соотношения сторон 2.5:1.
    src_w, src_h = source.size
    target_ratio = RENDER_WIDTH / RENDER_HEIGHT
    src_ratio = src_w / max(src_h, 1)
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        source = source.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        source = source.crop((0, top, src_w, top + new_h))
    return source.resize((RENDER_WIDTH, RENDER_HEIGHT))


def load_local_background_image() -> Image.Image:
    bg_path = os.getenv("COVER_BG_PATH", "").strip()
    if not bg_path:
        return build_default_background()

    if not os.path.exists(bg_path):
        return build_default_background()

    try:
        source = Image.open(bg_path)
    except OSError:
        return build_default_background()

    return normalize_background_image(source)


async def load_free_api_background_image(session: aiohttp.ClientSession) -> Image.Image | None:
    use_free_api = os.getenv("COVER_USE_FREE_API", "0") == "1"
    if not use_free_api:
        return None

    raw_primary = os.getenv("COVER_FREE_API_URL", DEFAULT_FREE_BG_API_URL).strip() or DEFAULT_FREE_BG_API_URL
    raw_fallbacks = os.getenv("COVER_FREE_API_FALLBACK_URLS", "").strip()
    urls: list[str] = [raw_primary]
    if raw_fallbacks:
        urls.extend([url.strip() for url in raw_fallbacks.split(",") if url.strip()])
    else:
        urls.extend(DEFAULT_FREE_BG_FALLBACK_URLS)

    for base_url in urls:
        if "{ts}" in base_url:
            request_url = base_url.replace("{ts}", str(int(datetime.now(MSK_TZ).timestamp())))
        else:
            separator = "&" if "?" in base_url else "?"
            request_url = f"{base_url}{separator}t={int(datetime.now(MSK_TZ).timestamp())}"

        try:
            async with session.get(request_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    continue
                content = await response.read()
        except Exception:
            continue

        try:
            source = Image.open(io.BytesIO(content))
            return normalize_background_image(source)
        except OSError:
            continue
    return None


def load_cover_fonts() -> tuple[ImageFont.ImageFont, ImageFont.ImageFont]:
    # Приоритет: переменная окружения -> шрифт в папке проекта -> arial.
    custom_font = os.getenv("COVER_FONT_PATH", "").strip()
    project_font = os.path.join(os.path.dirname(__file__), DEFAULT_COVER_FONT)

    font_candidates = [custom_font, project_font, "arial.ttf"]
    for font_path in font_candidates:
        if not font_path:
            continue
        try:
            date_font = ImageFont.truetype(font_path, 62)
            time_font = ImageFont.truetype(font_path, 126)
            return date_font, time_font
        except OSError:
            continue

    return ImageFont.load_default(), ImageFont.load_default()


def build_cover_png_bytes(msk_now: datetime, background_image: Image.Image) -> bytes:
    image = background_image
    draw = ImageDraw.Draw(image)
    date_font, time_font = load_cover_fonts()

    date_text = format_date_line_ru(msk_now)
    time_text = msk_now.strftime("%H:%M")

    # Ключевой текст размещаем в безопасной зоне 960x384,
    # которая реально отображается для личной страницы VK.
    safe_x1, safe_y1, safe_x2, safe_y2 = 0, 0, CROP_WIDTH, CROP_HEIGHT
    safe_center_x = (safe_x1 + safe_x2) // 2

    draw_centered_text(
        draw,
        safe_center_x,
        108,
        time_text,
        time_font,
        (255, 255, 255, 225),
        (170, 200, 255, 140),
        0,
    )
    draw_centered_text(
        draw,
        safe_center_x,
        208,
        date_text,
        date_font,
        (236, 243, 255, 210),
        (170, 200, 255, 120),
        0,
    )

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


async def vk_method(
    session: aiohttp.ClientSession,
    access_token: str,
    method: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {"access_token": access_token, "v": VK_API_VERSION, **params}
    url = f"{VK_API_BASE}{method}"

    async with session.post(url, data=payload) as response:
        raw_text = await response.text()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        snippet = raw_text[:300].replace("\n", " ").strip()
        raise RuntimeError(
            f"VK API non-JSON response in {method}: HTTP {response.status}, body='{snippet}'"
        ) from exc

    if "error" in data:
        error = data["error"]
        code = error.get("error_code")
        msg = error.get("error_msg")
        raise RuntimeError(f"VK API error in {method}: {code} {msg}")

    return data["response"]


async def upload_cover_photo(
    session: aiohttp.ClientSession,
    upload_url: str,
    png_bytes: bytes,
) -> Dict[str, Any]:
    form = aiohttp.FormData()
    form.add_field(
        "photo",
        png_bytes,
        filename="cover.png",
        content_type="image/png",
    )

    async with session.post(upload_url, data=form) as response:
        raw_text = await response.text()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        snippet = raw_text[:300].replace("\n", " ").strip()
        raise RuntimeError(
            f"Upload server non-JSON response: HTTP {response.status}, body='{snippet}'"
        ) from exc

    if "hash" not in data or "photo" not in data:
        raise RuntimeError(f"Unexpected upload response: {data}")

    return data


def get_next_minute_boundary_msk(now_msk: datetime) -> datetime:
    return (now_msk.replace(second=0, microsecond=0) + timedelta(minutes=1))


async def sleep_until_next_msk_minute() -> datetime:
    now_msk = datetime.now(MSK_TZ)
    next_boundary = get_next_minute_boundary_msk(now_msk)
    wait_seconds = max((next_boundary - now_msk).total_seconds(), 0.0)
    await asyncio.sleep(wait_seconds)
    return datetime.now(MSK_TZ).replace(second=0, microsecond=0)


def create_aiohttp_connector() -> aiohttp.TCPConnector:
    # Для проблем с корпоративными/локальными сертификатами:
    # - VK_SSL_NO_VERIFY=1 отключает проверку SSL (только для отладки).
    # - VK_CA_FILE=path/to/ca.pem задает кастомный CA bundle.
    no_verify = os.getenv("VK_SSL_NO_VERIFY", "0") == "1"
    ca_file = os.getenv("VK_CA_FILE")

    if no_verify:
        return aiohttp.TCPConnector(ssl=False)

    if ca_file:
        ssl_context = ssl.create_default_context(cafile=ca_file)
        return aiohttp.TCPConnector(ssl=ssl_context)

    return aiohttp.TCPConnector()


async def update_cover_once(
    session: aiohttp.ClientSession,
    access_token: str,
    user_id: int,
    msk_now: datetime,
) -> None:
    background_image = await load_free_api_background_image(session)
    if background_image is None:
        background_image = load_local_background_image()
    png_bytes = build_cover_png_bytes(msk_now, background_image)

    upload_server_params = {
        "user_id": user_id,
        "crop_x": 0,
        "crop_y": 0,
        # ВАЖНО: для личного профиля только 960x384.
        "crop_width": CROP_WIDTH,
        "crop_height": CROP_HEIGHT,
    }
    server = await vk_method(
        session,
        access_token,
        "photos.getOwnerCoverPhotoUploadServer",
        upload_server_params,
    )

    upload_data = await upload_cover_photo(session, server["upload_url"], png_bytes)

    save_params = {
        "user_id": user_id,
        "hash": upload_data["hash"],
        "photo": upload_data["photo"],
    }
    await vk_method(session, access_token, "photos.saveOwnerCoverPhoto", save_params)


async def main() -> None:
    load_dotenv()

    access_token = get_required_env("VK_USER_TOKEN")
    user_id = int(get_required_env("VK_USER_ID"))

    connector = create_aiohttp_connector()
    async with aiohttp.ClientSession(connector=connector) as session:
        print("Updater is running. Cover will refresh every minute by MSK clock.")
        while True:
            msk_now = await sleep_until_next_msk_minute()
            try:
                await update_cover_once(session, access_token, user_id, msk_now)
                print(f"Cover updated successfully at {msk_now.strftime('%H:%M:%S MSK')}.")
            except aiohttp.ClientConnectorCertificateError:
                print(
                    "SSL certificate error while connecting to VK. "
                    "Set VK_SSL_NO_VERIFY=1 in .env or configure VK_CA_FILE."
                )
            except Exception as exc:
                print(f"Cover update failed at {msk_now.strftime('%H:%M:%S MSK')}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
