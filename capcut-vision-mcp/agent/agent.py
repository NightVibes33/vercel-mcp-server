from __future__ import annotations

import base64
import io
import json
import logging
import os
import pathlib
import queue
import secrets
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from tkinter import messagebox
from typing import Any

import keyboard
import mss
import psutil
import pyautogui
import pyperclip
import requests
import win32con
import win32gui
import win32process
from PIL import Image

APP_NAME = "CapCut Vision MCP Agent"
CONFIG_PATH = pathlib.Path(__file__).with_name("config.json")
LOG_PATH = pathlib.Path(__file__).with_name("agent.log")
KILL_HOTKEY = "ctrl+shift+f12"
MAX_FILE_RESULTS = 200
MAX_ACTIONS_PER_BATCH = 100

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


@dataclass
class AgentConfig:
    relay_url: str
    agent_id: str
    pairing_token: str
    poll_seconds: float
    jpeg_quality: int
    capcut_exe: str
    allowed_roots: list[pathlib.Path]

    @classmethod
    def load(cls) -> "AgentConfig":
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"Missing {CONFIG_PATH.name}. Copy config.example.json to config.json first."
            )
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        roots = [
            pathlib.Path(os.path.expandvars(os.path.expanduser(value))).resolve()
            for value in raw.get("allowed_roots", [])
        ]
        return cls(
            relay_url=str(raw["relay_url"]).rstrip("/"),
            agent_id=str(raw.get("agent_id") or f"windows-{secrets.token_hex(4)}"),
            pairing_token=str(raw["pairing_token"]),
            poll_seconds=max(0.25, float(raw.get("poll_seconds", 1.0))),
            jpeg_quality=max(35, min(95, int(raw.get("jpeg_quality", 72)))),
            capcut_exe=str(raw.get("capcut_exe", "")),
            allowed_roots=roots,
        )


class SecurityError(RuntimeError):
    pass


class DesktopController:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def _ensure_allowed_path(self, value: str) -> pathlib.Path:
        candidate = pathlib.Path(os.path.expandvars(os.path.expanduser(value))).resolve()
        for root in self.config.allowed_roots:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        raise SecurityError(f"Path is outside approved roots: {candidate}")

    def screenshot(self, monitor: int = 0) -> dict[str, Any]:
        with mss.mss() as capture:
            monitors = capture.monitors
            index = monitor if 0 <= monitor < len(monitors) else 0
            shot = capture.grab(monitors[index])
            image = Image.frombytes("RGB", shot.size, shot.rgb)
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=self.config.jpeg_quality, optimize=True)
            return {
                "mime": "image/jpeg",
                "width": image.width,
                "height": image.height,
                "base64": base64.b64encode(out.getvalue()).decode("ascii"),
            }

    def list_windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []

        def callback(hwnd: int, _: Any) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                process = psutil.Process(pid)
                rect = win32gui.GetWindowRect(hwnd)
                windows.append(
                    {
                        "hwnd": hwnd,
                        "title": title,
                        "pid": pid,
                        "process": process.name(),
                        "rect": {
                            "left": rect[0],
                            "top": rect[1],
                            "right": rect[2],
                            "bottom": rect[3],
                        },
                    }
                )
            except Exception:
                return

        win32gui.EnumWindows(callback, None)
        return windows

    def focus_window(self, title_contains: str | None = None, hwnd: int | None = None) -> dict[str, Any]:
        target = hwnd
        if target is None and title_contains:
            needle = title_contains.casefold()
            for item in self.list_windows():
                if needle in item["title"].casefold() or needle in item["process"].casefold():
                    target = int(item["hwnd"])
                    break
        if target is None:
            raise RuntimeError("Window not found.")
        win32gui.ShowWindow(target, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(target)
        except Exception:
            pyautogui.press("alt")
            win32gui.SetForegroundWindow(target)
        return {"focused": target, "title": win32gui.GetWindowText(target)}

    def open_capcut(self) -> dict[str, Any]:
        for window in self.list_windows():
            if "capcut" in window["title"].casefold() or "capcut" in window["process"].casefold():
                return self.focus_window(hwnd=int(window["hwnd"]))

        candidates = [
            self.config.capcut_exe,
            os.path.expandvars(r"%LOCALAPPDATA%\CapCut\Apps\CapCut.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\CapCut\CapCut.exe"),
        ]
        for candidate in candidates:
            if candidate and pathlib.Path(candidate).exists():
                subprocess.Popen([candidate], close_fds=True)
                return {"launched": candidate}
        raise FileNotFoundError("CapCut.exe was not found. Set capcut_exe in config.json.")

    def list_directory(self, path: str) -> list[dict[str, Any]]:
        root = self._ensure_allowed_path(path)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        items = []
        for child in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))[:MAX_FILE_RESULTS]:
            try:
                stat = child.stat()
                items.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_dir": child.is_dir(),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
            except OSError:
                continue
        return items

    def search_files(self, query: str, extensions: list[str] | None = None) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        allowed_ext = {value.casefold() if value.startswith(".") else f".{value.casefold()}" for value in (extensions or [])}
        results: list[dict[str, Any]] = []
        for root in self.config.allowed_roots:
            if not root.exists():
                continue
            for directory, _, files in os.walk(root):
                for name in files:
                    if needle and needle not in name.casefold():
                        continue
                    path = pathlib.Path(directory, name)
                    if allowed_ext and path.suffix.casefold() not in allowed_ext:
                        continue
                    try:
                        stat = path.stat()
                        results.append({"name": name, "path": str(path), "size": stat.st_size, "modified": stat.st_mtime})
                    except OSError:
                        continue
                    if len(results) >= MAX_FILE_RESULTS:
                        return results
        return results

    def execute(self, action: dict[str, Any]) -> Any:
        name = str(action.get("type", "")).strip()
        logging.info("Executing action: %s", name)

        if name == "screenshot":
            return self.screenshot(int(action.get("monitor", 0)))
        if name == "list_windows":
            return self.list_windows()
        if name == "focus_window":
            return self.focus_window(action.get("title_contains"), action.get("hwnd"))
        if name == "click":
            pyautogui.click(float(action["x"]), float(action["y"]), clicks=int(action.get("clicks", 1)), interval=float(action.get("interval", 0.1)))
            return {"ok": True}
        if name == "double_click":
            pyautogui.doubleClick(float(action["x"]), float(action["y"]), interval=float(action.get("interval", 0.12)))
            return {"ok": True}
        if name == "right_click":
            pyautogui.rightClick(float(action["x"]), float(action["y"]))
            return {"ok": True}
        if name == "move":
            pyautogui.moveTo(float(action["x"]), float(action["y"]), duration=float(action.get("duration", 0.2)))
            return {"ok": True}
        if name == "drag":
            pyautogui.moveTo(float(action["from_x"]), float(action["from_y"]), duration=0.1)
            pyautogui.dragTo(float(action["to_x"]), float(action["to_y"]), duration=float(action.get("duration", 0.5)), button=str(action.get("button", "left")))
            return {"ok": True}
        if name == "scroll":
            pyautogui.scroll(int(action["amount"]), x=action.get("x"), y=action.get("y"))
            return {"ok": True}
        if name == "type_text":
            text = str(action.get("text", ""))
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            return {"ok": True, "characters": len(text)}
        if name == "press_key":
            pyautogui.press(str(action["key"]), presses=int(action.get("presses", 1)), interval=float(action.get("interval", 0.05)))
            return {"ok": True}
        if name == "hotkey":
            keys = action.get("keys")
            if not isinstance(keys, list) or not keys:
                raise ValueError("hotkey requires a non-empty keys array")
            pyautogui.hotkey(*[str(key) for key in keys])
            return {"ok": True}
        if name == "open_capcut":
            return self.open_capcut()
        if name == "open_explorer":
            path = self._ensure_allowed_path(str(action.get("path") or self.config.allowed_roots[0]))
            os.startfile(path)
            return {"opened": str(path)}
        if name == "open_path":
            path = self._ensure_allowed_path(str(action["path"]))
            os.startfile(path)
            return {"opened": str(path)}
        if name == "open_url":
            url = str(action["url"])
            if not url.startswith(("https://", "http://")):
                raise SecurityError("Only http and https URLs are allowed.")
            webbrowser.open(url)
            return {"opened": url}
        if name == "list_directory":
            return self.list_directory(str(action["path"]))
        if name == "search_files":
            return self.search_files(str(action.get("query", "")), action.get("extensions"))
        if name == "file_info":
            path = self._ensure_allowed_path(str(action["path"]))
            stat = path.stat()
            return {"path": str(path), "name": path.name, "is_dir": path.is_dir(), "size": stat.st_size, "modified": stat.st_mtime}
        if name == "import_into_capcut":
            path = self._ensure_allowed_path(str(action["path"]))
            self.open_capcut()
            time.sleep(float(action.get("launch_wait", 2.0)))
            pyautogui.hotkey("ctrl", "i")
            time.sleep(0.6)
            pyperclip.copy(str(path))
            pyautogui.hotkey("ctrl", "l")
            pyautogui.hotkey("ctrl", "v")
            pyautogui.press("enter")
            return {"import_requested": str(path)}
        if name == "wait":
            seconds = max(0.0, min(30.0, float(action.get("seconds", 1.0))))
            time.sleep(seconds)
            return {"waited": seconds}
        raise ValueError(f"Unsupported action: {name}")


class RelayClient:
    def __init__(self, config: AgentConfig, controller: DesktopController) -> None:
        self.config = config
        self.controller = controller
        self.stop_event = threading.Event()
        self.connected = False
        self.status_queue: queue.Queue[str] = queue.Queue()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.pairing_token}",
                "User-Agent": f"{APP_NAME}/1.0",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.config.relay_url}{path}"

    def connect(self) -> None:
        payload = {
            "agent_id": self.config.agent_id,
            "platform": "windows",
            "hostname": os.environ.get("COMPUTERNAME", "Windows PC"),
            "capabilities": [
                "vision",
                "mouse",
                "keyboard",
                "windows",
                "approved_files",
                "capcut",
            ],
        }
        response = self.session.post(self._url("/agent/connect"), json=payload, timeout=20)
        response.raise_for_status()
        self.connected = True
        self.status_queue.put("AI CONNECTED")
        logging.info("Connected to relay as %s", self.config.agent_id)

    def post_result(self, command_id: str, result: Any = None, error: str | None = None) -> None:
        payload = {"agent_id": self.config.agent_id, "command_id": command_id, "result": result, "error": error}
        response = self.session.post(self._url("/agent/result"), json=payload, timeout=60)
        response.raise_for_status()

    def run(self) -> None:
        try:
            self.connect()
            while not self.stop_event.is_set():
                try:
                    response = self.session.get(
                        self._url("/agent/next"),
                        params={"agent_id": self.config.agent_id},
                        timeout=35,
                    )
                    if response.status_code == 204:
                        continue
                    response.raise_for_status()
                    command = response.json()
                    command_id = str(command["command_id"])
                    actions = command.get("actions") or [command.get("action")]
                    actions = [item for item in actions if isinstance(item, dict)]
                    if len(actions) > MAX_ACTIONS_PER_BATCH:
                        raise SecurityError("Too many actions in one batch.")
                    results = [self.controller.execute(action) for action in actions]
                    self.post_result(command_id, result=results)
                except requests.Timeout:
                    continue
                except Exception as exc:
                    logging.exception("Command failed")
                    try:
                        command_id = str(locals().get("command", {}).get("command_id", "unknown"))
                        self.post_result(command_id, error=str(exc))
                    except Exception:
                        pass
                    self.status_queue.put(f"Command failed: {exc}")
                    time.sleep(self.config.poll_seconds)
        except Exception as exc:
            logging.exception("Relay connection failed")
            self.status_queue.put(f"Disconnected: {exc}")
        finally:
            self.connected = False

    def stop(self) -> None:
        self.stop_event.set()
        self.connected = False
        self.status_queue.put("PAUSED")


class AgentWindow:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.controller = DesktopController(config)
        self.client = RelayClient(config, self.controller)
        self.thread: threading.Thread | None = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.configure(bg="#0b0d12")
        self.root.protocol("WM_DELETE_WINDOW", self.exit_agent)

        self.status = tk.StringVar(value="DISCONNECTED")
        self.status_label = tk.Label(
            self.root,
            textvariable=self.status,
            bg="#0b0d12",
            fg="#fda4af",
            font=("Segoe UI", 16, "bold"),
            padx=24,
            pady=12,
        )
        self.status_label.pack(fill="x")

        tk.Label(
            self.root,
            text=f"Agent: {config.agent_id}\nKill switch: {KILL_HOTKEY}",
            bg="#0b0d12",
            fg="#cbd5e1",
            font=("Segoe UI", 10),
            padx=24,
            pady=6,
        ).pack()

        buttons = tk.Frame(self.root, bg="#0b0d12", padx=16, pady=16)
        buttons.pack()
        tk.Button(buttons, text="Connect", command=self.start, width=12, bg="#22c55e", fg="#07110a").grid(row=0, column=0, padx=4)
        tk.Button(buttons, text="Pause", command=self.pause, width=12, bg="#f59e0b", fg="#1c1000").grid(row=0, column=1, padx=4)
        tk.Button(buttons, text="Exit", command=self.exit_agent, width=12, bg="#ef4444", fg="white").grid(row=0, column=2, padx=4)

        keyboard.add_hotkey(KILL_HOTKEY, self.pause)
        self.root.after(250, self.refresh_status)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.client = RelayClient(self.config, self.controller)
        self.thread = threading.Thread(target=self.client.run, name="relay-client", daemon=True)
        self.thread.start()
        self.status.set("CONNECTING…")
        self.status_label.configure(fg="#fde68a")

    def pause(self) -> None:
        self.client.stop()
        self.status.set("PAUSED")
        self.status_label.configure(fg="#fde68a")
        logging.warning("Remote control paused by local user")

    def refresh_status(self) -> None:
        try:
            while True:
                message = self.client.status_queue.get_nowait()
                self.status.set(message)
                if message == "AI CONNECTED":
                    self.status_label.configure(fg="#4ade80")
                elif message == "PAUSED":
                    self.status_label.configure(fg="#fde68a")
                else:
                    self.status_label.configure(fg="#fda4af")
        except queue.Empty:
            pass
        self.root.after(250, self.refresh_status)

    def exit_agent(self) -> None:
        self.client.stop()
        keyboard.unhook_all_hotkeys()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    try:
        config = AgentConfig.load()
        AgentWindow(config).run()
    except Exception as exc:
        logging.exception("Agent startup failed")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, str(exc))
        raise


if __name__ == "__main__":
    main()
