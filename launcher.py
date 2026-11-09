"""Оконный запуск SZAUCTRACKER для exe.

Поднимает локальный сервер в фоне, открывает браузер и показывает небольшое
окно управления. В exe нет консоли, поэтому весь вывод пишется в data/launcher.log.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

import app as core

LOG_PATH = core.DATA_DIR / "launcher.log"
_log_file = None


def setup_logging() -> None:
    """В windowed-режиме sys.stdout может быть None — уводим вывод в файл."""
    global _log_file
    try:
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            backup = LOG_PATH.with_name(LOG_PATH.name + ".1")
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            LOG_PATH.replace(backup)
        _log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = _log_file
        sys.stderr = _log_file
    except OSError:
        _log_file = None


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        if _log_file is not None:
            _log_file.write(line + "\n")
    except OSError:
        pass


def http_json(url: str, timeout: float = 1.5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def existing_instance() -> str | None:
    """Если приложение уже запущено на стандартном порту — вернуть его URL."""
    url = "http://127.0.0.1:8765"
    try:
        data = http_json(url + "/api/health", timeout=0.8)
        if data.get("ok") is True:
            return url
    except Exception:
        return None
    return None


BG = "#0b0f0d"
PANEL = "#131a16"
LINE = "#24312a"
TEXT = "#e7efe9"
MUTED = "#8ba093"
ACCENT = "#a8e05f"
RED = "#e2675a"
ORANGE = "#e8a94a"


class LauncherWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("SZAUCTRACKER 1.9")
        self.root.configure(bg=BG)
        self.root.geometry("470x310")
        self.root.minsize(430, 300)
        try:
            icon = core.RESOURCE_DIR / "assets" / "icon.ico"
            if icon.exists():
                self.root.iconbitmap(default=str(icon))
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.server = None
        self._poll_job = None
        self._build_ui()
        self.root.after(120, self.start_server)
        self.root.after(1100, self.open_interface)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=22, pady=(20, 6))

        mark = tk.Label(
            header, text="SZ", bg=ACCENT, fg="#0b1406",
            font=("Segoe UI", 16, "bold"), width=3, height=1,
        )
        mark.pack(side="left", padx=(0, 12))

        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text="SZAUCTRACKER", bg=BG, fg=TEXT,
                 font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tk.Label(titles, text="сканер аукциона STALZONE · v1.9", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")

        divider = tk.Frame(self.root, bg=LINE, height=1)
        divider.pack(fill="x", padx=22, pady=(14, 12))

        status_row = tk.Frame(self.root, bg=BG)
        status_row.pack(fill="x", padx=22)
        self.status_dot = tk.Label(status_row, text="●", bg=BG, fg=ORANGE,
                                   font=("Segoe UI", 12))
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(status_row, text="Запуск сервера…", bg=BG, fg=TEXT,
                                     font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side="left", padx=(6, 0))

        self.url_label = tk.Label(self.root, text="…", bg=BG, fg=ACCENT,
                                  font=("Consolas", 10), cursor="hand2")
        self.url_label.pack(anchor="w", padx=22, pady=(4, 0))
        self.url_label.bind("<Button-1>", lambda _event: self.open_interface())

        self.info_label = tk.Label(self.root, text="Интерфейс откроется в браузере.",
                                   bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.info_label.pack(anchor="w", padx=22, pady=(2, 14))

        buttons = tk.Frame(self.root, bg=BG)
        buttons.pack(fill="x", padx=22)

        self.open_btn = self._button(buttons, "Открыть интерфейс", self.open_interface, primary=True)
        self.open_btn.pack(side="left")
        self._button(buttons, "Папка данных", self.open_data).pack(side="left", padx=8)
        self._button(buttons, "Выход", self.quit).pack(side="right")

        hint = tk.Label(
            self.root,
            text="Закройте это окно, чтобы остановить приложение.\nДанные и ключи хранятся в папке data рядом с exe.",
            bg=BG, fg="#64786b", font=("Segoe UI", 8), justify="left",
        )
        hint.pack(anchor="w", padx=22, pady=(18, 0))

    def _button(self, parent, text, command, primary=False):
        bg = ACCENT if primary else PANEL
        fg = "#0b1406" if primary else TEXT
        active = "#c6ff7a" if primary else "#1b2620"
        return tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=active, activeforeground=fg,
            relief="flat", bd=0, padx=14, pady=7,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        )

    # ------------------------------------------------------------------ сервер

    def start_server(self) -> None:
        try:
            self.server = core.AppServer(port=8765).start()
            self.url_label.config(text=self.server.url, cursor="hand2")
            self._set_status("Сервер работает", ACCENT)
            self.info_label.config(text="Интерфейс откроется в браузере.")
            self._schedule_poll(300)
            log(f"Сервер запущен: {self.server.url}")
        except Exception as exc:
            log("Не удалось запустить сервер:\n" + traceback.format_exc())
            self._set_status("Ошибка запуска", RED)
            self.info_label.config(text=str(exc))
            messagebox.showerror(
                "SZAUCTRACKER",
                f"Не удалось запустить локальный сервер.\n\n{exc}\n\nПодробности: {LOG_PATH}",
            )

    def open_interface(self) -> None:
        if self.server:
            webbrowser.open(self.server.url)
        else:
            webbrowser.open("http://127.0.0.1:8765")

    def open_data(self) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(core.DATA_DIR)  # type: ignore[attr-defined]
            else:
                webbrowser.open(core.DATA_DIR.as_uri())
        except OSError as exc:
            messagebox.showerror("SZAUCTRACKER", f"Не удалось открыть папку:\n{exc}")

    # ------------------------------------------------------------------ статус

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.config(text=text, fg=TEXT)
        self.status_dot.config(fg=color)

    def _schedule_poll(self, delay: int = 2500) -> None:
        self._poll_job = self.root.after(delay, self.poll)

    def poll(self) -> None:
        if not self.server:
            return
        try:
            status = http_json(self.server.url + "/api/status", timeout=1.2)
            if status.get("running"):
                total = status.get("total") or 0
                done = status.get("done") or 0
                self._set_status(f"Сканирование: {done}/{total}", ORANGE)
                self.info_label.config(text=f"Текущий предмет: {status.get('current') or '—'}")
            elif status.get("error"):
                self._set_status("Ошибка скана", RED)
                self.info_label.config(text=str(status["error"])[:90])
            else:
                meta = status.get("results_meta") or {}
                if meta.get("generated_at"):
                    self._set_status("Сервер работает", ACCENT)
                    self.info_label.config(
                        text=f"Сделок: {meta.get('deals_count', 0)} · предметов: {meta.get('items_total', 0)} · "
                             f"{self._short_time(meta.get('generated_at'))}"
                    )
                else:
                    self._set_status("Сервер работает", ACCENT)
                    self.info_label.config(text="Сканов ещё не было — откройте интерфейс.")
        except Exception as exc:
            self._set_status("Сервер не отвечает", RED)
            self.info_label.config(text=str(exc)[:90])
        finally:
            self._schedule_poll(2500)

    @staticmethod
    def _short_time(iso: str) -> str:
        try:
            return iso[11:19]
        except Exception:
            return ""

    # ------------------------------------------------------------------ выход

    def quit(self) -> None:
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        if self.server is not None:
            self._set_status("Остановка…", ORANGE)
            try:
                self.server.stop()
            except Exception:
                log("Ошибка остановки:\n" + traceback.format_exc())
            self.server = None
        log("Приложение закрыто")
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if hasattr(sys, "frozen"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    setup_logging()
    log("=== запуск ===")

    running = existing_instance()
    if running:
        log(f"Уже запущено: {running}")
        webbrowser.open(running)
        messagebox.showinfo(
            "SZAUCTRACKER",
            "Приложение уже запущено — открываю интерфейс в браузере.",
        )
        return 0

    window = LauncherWindow()
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
