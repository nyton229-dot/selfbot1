# VK Bot

## Быстрый запуск (Windows/Linux)

```bash
pip install -r requirements.txt
python bot.py
```

Заполни `.env`:

- `VK_USER_TOKEN`
- `VK_USER_ID`
- `AI_PROVIDER=bothub`
- `BOTHUB_API_KEY`

Если все ок, увидишь:

`Bot is running and listening for events...`

## Интерфейс Старт/Стоп (Windows)

Если хочешь запускать через окно с кнопками:

```bash
python bot_control_panel.py
```

Что умеет панель:

- кнопки `Старт` и `Стоп` для `bot.py`;
- статус `Включен/Выключен`;
- вывод лога бота в реальном времени;
- всплывающее уведомление, если бот отключился сам (не по кнопке).

## Автоперезапуск при падении

Если хочешь, чтобы бот сам поднимался после падения:

```bash
python bot_watchdog.py
```

Watchdog запускает `bot.py` и, если процесс завершается, автоматически стартует его заново.
Пауза между рестартами задается переменной:

```env
BOT_RESTART_DELAY_SEC=2.0
```

## Запуск с телефона (Pydroid 3)

1. Открой проект в Pydroid (папка с `bot.py`).
2. Установи зависимости:

```bash
pip install -r requirements.txt
```

3. Для телефона создай `mobile.env` (можно скопировать из `mobile.env.example`) и заполни:

```env
VK_USER_TOKEN=...
VK_USER_ID=...
AI_PROVIDER=bothub
BOTHUB_API_KEY=...
BOTHUB_BASE_URL=https://openai.bothub.chat/v1
BOT_ENABLE_COVER_UPDATER=0
```

4. Запусти:

```bash
python bot.py
```

### Важно для телефона

- В коде уже добавлен phone-friendly режим:
  - на Pydroid сначала грузится `mobile.env`, потом fallback на `.env`;
  - cover updater по умолчанию отключается на Pydroid (можно включить через `BOT_ENABLE_COVER_UPDATER=1`).
- Не меняй сеть между выдачей VK токена и запуском бота.
- Можно явно выбрать env-файл через переменную `BOT_ENV_FILE` (например `BOT_ENV_FILE=mobile.env`).

## Деплой на Bothost.ru (24/7)

Bothost сам собирает Docker-образ из Git: нужны `bot.py`, `requirements.txt` и `Procfile`.

### 1. Залей код в GitHub/GitLab

В репозиторий **не** попадают `.env`, `mobile.env` и `saved_gs/` — они в `.gitignore`.

Минимальный набор файлов:

- `bot.py`, `bot_watchdog.py`, `profile_cover_updater.py`
- `bothub_prompt.txt`, `ollama_prompt.txt`
- `requirements.txt`, `Procfile`, `README.md`

Локально (один раз):

```bash
git init
git add .
git commit -m "VK bot for Bothost"
git branch -M main
git remote add origin https://github.com/ТВОЙ_ЛОГИН/vkbot.git
git push -u origin main
```

### 2. Создай бота на Bothost

1. Зарегистрируйся на [bothost.ru](https://bothost.ru/).
2. Создай проект → платформа **VK**.
3. Укажи Git URL репозитория и ветку `main`.
4. В разделе **Переменные окружения** добавь (без кавычек):

```env
VK_USER_TOKEN=твой_токен_vk
VK_USER_ID=твой_id
AI_PROVIDER=bothub
BOTHUB_API_KEY=твой_ключ_bothub
BOTHUB_BASE_URL=https://openai.bothub.chat/v1
BOT_ENABLE_COVER_UPDATER=0
```

Опционально: `BOTHUB_MODEL`, `BOTHUB_VISION_MODEL`, `BOT_RESTART_DELAY_SEC=2.0`.

5. Запусти деплой и смотри логи в панели.

В логах должно появиться:

`Bot is running and listening for events...`

`Procfile` запускает `bot_watchdog.py` — при падении бот перезапустится сам.

### 3. VK-токен и IP

Токен VK часто привязан к IP. После деплоя на Bothost **выпусти новый токен** с того же аккаунта и обнови `VK_USER_TOKEN` в панели Bothost (без перезаливки кода).

### 4. Обновления

Любой `git push` в ветку `main` — Bothost пересоберёт и перезапустит бота автоматически.
