"""
vrchat-mouserun
マウスの左クリック＋右クリック同時押し中、VRChatがアクティブなら W+LShift を送信する。
タスクトレイに常駐。右クリックメニューで自動起動の管理と終了が可能。

【ビルド方法】
  pip install -r requirements.txt
  pyinstaller main.py --onefile --noconsole --name vrchat-mouserun ^
    --hidden-import pynput.mouse._win32 ^
    --hidden-import pynput.keyboard._win32
"""
import atexit
import ctypes
import ctypes.wintypes
import logging
import os
import queue
import signal
import sys
import threading
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pynput import mouse

# ── ログ設定 ──────────────────────────────────────────────
def _setup_logging() -> None:
    if getattr(sys, "frozen", False):
        log_dir = Path(os.environ.get("APPDATA", Path.home())) / "vrchat-mouserun"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_dir / "vrchat-mouserun.log"), encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler],
    )

_setup_logging()
logger = logging.getLogger(__name__)

# ── Win32 定数 ────────────────────────────────────────────
VK_W       = 0x57
VK_LSHIFT  = 0xA0
SC_W       = 0x11
SC_LSHIFT  = 0x2A
KEYEVENTF_KEYUP    = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD     = 1

# ── ctypes 構造体 ─────────────────────────────────────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki",   KEYBDINPUT),
        ("_pad", ctypes.c_byte * 32),
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type",   ctypes.wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]

user32 = ctypes.windll.user32
user32.SendInput.argtypes = [
    ctypes.wintypes.UINT,
    ctypes.POINTER(INPUT),
    ctypes.c_int,
]
user32.SendInput.restype = ctypes.wintypes.UINT

# ── 状態管理 ──────────────────────────────────────────────
_lock       = threading.Lock()
_left_down  = False
_right_down = False
_forwarding = False
_stop_event = threading.Event()
_call_queue: queue.SimpleQueue = queue.SimpleQueue()


def is_vrchat_active() -> bool:
    hwnd = user32.GetForegroundWindow()
    buf  = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip() == "VRChat"


def send_key(vk: int, scan: int, down: bool) -> None:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if not down else 0)
    inp = INPUT(
        type=INPUT_KEYBOARD,
        _input=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        logger.warning("SendInput failed: sent=%d LastError=%d", sent, ctypes.GetLastError())


def _start_forward() -> None:
    if not is_vrchat_active():
        logger.warning("focus changed before SendInput, aborting start")
        return
    logger.info("forward start")
    send_key(VK_LSHIFT, SC_LSHIFT, True)
    send_key(VK_W,      SC_W,      True)


def _stop_forward() -> None:
    logger.info("forward stop")
    send_key(VK_W,      SC_W,      False)
    send_key(VK_LSHIFT, SC_LSHIFT, False)


def update_forward() -> None:
    global _forwarding
    with _lock:
        left, right = _left_down, _right_down
        forwarding   = _forwarding

    should_forward = left and right and is_vrchat_active()

    if should_forward and not forwarding:
        with _lock:
            _forwarding = True
        _start_forward()
    elif not should_forward and forwarding:
        with _lock:
            _forwarding = False
        _stop_forward()


def _worker() -> None:
    while not _stop_event.is_set():
        try:
            _call_queue.get(timeout=0.1)
            update_forward()
        except queue.Empty:
            update_forward()


def on_click(x, y, button, pressed) -> None:
    global _left_down, _right_down
    with _lock:
        if button == mouse.Button.left:
            _left_down = pressed
        elif button == mouse.Button.right:
            _right_down = pressed
    _call_queue.put_nowait(None)


def _emergency_cleanup() -> None:
    _stop_event.set()
    with _lock:
        forwarding = _forwarding
    if forwarding:
        _stop_forward()


# ── 自動起動（レジストリ） ────────────────────────────────
_STARTUP_REG_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME        = "vrchat-mouserun"


def _exe_path() -> str:
    """実行中のexeまたはスクリプトの絶対パスを返す。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(__file__).resolve())


def is_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY) as key:
            winreg.QueryValueEx(key, _APP_NAME)
            return True
    except (FileNotFoundError, OSError):
        return False


def set_startup(enabled: bool) -> None:
    try:
        if enabled:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, _exe_path())
            logger.info("Startup enabled: %s", _exe_path())
        else:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, _APP_NAME)
            logger.info("Startup disabled")
    except OSError as e:
        logger.error("Failed to update startup registry: %s", e)


# ── タスクトレイアイコン ───────────────────────────────────
def _create_tray_icon() -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 2, size - 2], fill=(0, 120, 215, 255))
    draw.polygon([(22, 16), (22, 48), (48, 32)], fill=(255, 255, 255, 255))
    return img


def _on_toggle_startup(icon: pystray.Icon, item) -> None:
    new_state = not is_startup_enabled()
    set_startup(new_state)
    icon.update_menu()


def _on_tray_exit(icon: pystray.Icon, item) -> None:
    logger.info("Tray exit requested")
    _stop_event.set()
    icon.stop()


def main() -> None:
    atexit.register(_emergency_cleanup)
    signal.signal(signal.SIGINT, lambda s, f: (_stop_event.set(),))

    logger.info("vrchat-mouserun started INPUT size=%d", ctypes.sizeof(INPUT))

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    listener = mouse.Listener(on_click=on_click)
    listener.start()

    tray = pystray.Icon(
        "vrchat-mouserun",
        _create_tray_icon(),
        "VRChat MouseRun",
        menu=pystray.Menu(
            pystray.MenuItem(
                "Windows起動時に自動起動",
                _on_toggle_startup,
                checked=lambda item: is_startup_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", _on_tray_exit),
        ),
    )

    try:
        tray.run()          # メインスレッドをブロック（Exit で icon.stop() → ここを抜ける）
    finally:
        _stop_event.set()
        listener.stop()

    logger.info("vrchat-mouserun stopped")


if __name__ == "__main__":
    main()
