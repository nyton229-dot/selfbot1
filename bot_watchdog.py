"""Сторож для бота: запускает bot.py и перезапускает его при падении.

Используется в Procfile для 24/7-хостинга (Bothost и т.п.).
Пауза между рестартами настраивается переменной BOT_RESTART_DELAY_SEC.
"""

import os
import subprocess
import sys
import time

RESTART_DELAY_SEC = float(os.getenv("BOT_RESTART_DELAY_SEC", "3.0"))
BOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")


def main() -> None:
    while True:
        print(f"[watchdog] Запускаю {BOT_PATH}", flush=True)
        process = subprocess.Popen([sys.executable, "-u", BOT_PATH])
        exit_code = process.wait()
        print(
            f"[watchdog] Бот завершился с кодом {exit_code}, "
            f"перезапуск через {RESTART_DELAY_SEC} сек...",
            flush=True,
        )
        time.sleep(RESTART_DELAY_SEC)


if __name__ == "__main__":
    main()
