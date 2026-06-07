import os
import subprocess
import sys
import time
from datetime import datetime


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    project_dir = os.path.dirname(os.path.abspath(__file__))
    bot_path = os.path.join(project_dir, "bot.py")
    if not os.path.exists(bot_path):
        print(f"[{ts()}] bot.py not found: {bot_path}")
        return

    restart_delay = float(os.getenv("BOT_RESTART_DELAY_SEC", "2.0"))
    print(f"[{ts()}] Watchdog started. Restart delay: {restart_delay}s")

    while True:
        print(f"[{ts()}] Starting bot.py ...")
        process = subprocess.Popen(
            [sys.executable, bot_path],
            cwd=project_dir,
        )
        exit_code = process.wait()
        print(f"[{ts()}] bot.py exited with code {exit_code}")

        # Graceful stop from external tools usually returns 0 or termination code.
        # Watchdog still restarts unless user stops watchdog itself.
        print(f"[{ts()}] Restarting in {restart_delay}s ...")
        time.sleep(restart_delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Watchdog stopped by user.")
