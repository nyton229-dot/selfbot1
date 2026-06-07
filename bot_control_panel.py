import os
import subprocess
import sys
import threading
import queue
from datetime import datetime
import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText


class BotControlPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("VK Bot Control Panel")
        self.root.geometry("860x560")

        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        self.bot_path = os.path.join(self.project_dir, "bot.py")

        self.process: subprocess.Popen | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.user_requested_stop = False

        self._build_ui()
        self._set_status("Выключен", "red")

        self.root.after(200, self._drain_output_queue)
        self.root.after(1000, self._check_process_state)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top_frame = tk.Frame(self.root)
        top_frame.pack(fill="x", padx=10, pady=10)

        self.start_button = tk.Button(top_frame, text="▶ Старт", width=14, command=self.start_bot)
        self.start_button.pack(side="left", padx=(0, 8))

        self.stop_button = tk.Button(top_frame, text="■ Стоп", width=14, command=self.stop_bot, state="disabled")
        self.stop_button.pack(side="left")

        self.status_label = tk.Label(top_frame, text="", font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side="right")

        info = tk.Label(
            self.root,
            text="Лог бота (в реальном времени):",
            anchor="w",
            justify="left",
        )
        info.pack(fill="x", padx=10)

        self.log_area = ScrolledText(self.root, wrap="word", font=("Consolas", 10))
        self.log_area.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_area.configure(state="disabled")

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.configure(text=f"Статус: {text}", fg=color)

    def _append_log(self, text: str) -> None:
        self.log_area.configure(state="normal")
        self.log_area.insert("end", text)
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def start_bot(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if not os.path.exists(self.bot_path):
            messagebox.showerror("Ошибка", f"Не найден файл бота:\n{self.bot_path}")
            return

        self.user_requested_stop = False
        self._append_log(f"[{self._timestamp()}] Запуск бота...\n")
        try:
            self.process = subprocess.Popen(
                [sys.executable, self.bot_path],
                cwd=self.project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
        except Exception as exc:
            messagebox.showerror("Ошибка запуска", f"Не удалось запустить бота:\n{exc}")
            return

        self._set_status("Включен", "green")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        reader = threading.Thread(target=self._read_process_output, daemon=True)
        reader.start()

    def stop_bot(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.user_requested_stop = True
        self._append_log(f"[{self._timestamp()}] Остановка бота...\n")
        try:
            self.process.terminate()
        except Exception:
            pass

    def _read_process_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.output_queue.put(line)

    def _drain_output_queue(self) -> None:
        while not self.output_queue.empty():
            line = self.output_queue.get_nowait()
            self._append_log(line)
        self.root.after(200, self._drain_output_queue)

    def _check_process_state(self) -> None:
        running = self.process is not None and self.process.poll() is None
        if running:
            self._set_status("Включен", "green")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        else:
            if self.process is not None:
                code = self.process.poll()
                self._append_log(f"[{self._timestamp()}] Бот остановлен (код выхода: {code}).\n")
                if not self.user_requested_stop:
                    messagebox.showwarning(
                        "Бот выключился",
                        f"Бот отключился сам.\nКод выхода: {code}\nПроверь лог в окне панели.",
                    )
                self.process = None
            self._set_status("Выключен", "red")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
        self.root.after(1000, self._check_process_state)

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            answer = messagebox.askyesno("Выход", "Бот запущен. Остановить бота и закрыть панель?")
            if not answer:
                return
            self.user_requested_stop = True
            try:
                self.process.terminate()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    BotControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
