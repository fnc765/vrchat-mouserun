"""
vrchat-mouserun
マウスの左クリック＋右クリック同時押し中、VRChatがアクティブなら W+LShift を送信する。

【ビルド方法】
  pip install -r requirements.txt
  pyinstaller main.py --onefile --noconsole ^
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
import time
from pathlib import Path
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
SC_W       = 0x11   # W キーのスキャンコード
SC_LSHIFT  = 0x2A   # LShift キーのスキャンコード
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
_call_queue: queue.SimpleQueue = queue.SimpleQueue()  # フックスレッドから切り離すためのキュー


def is_vrchat_active() -> bool:
    hwnd = user32.GetForegroundWindow()
    buf  = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip() == "VRChat"


def send_key(vk: int, scan: int, down: bool) -> None:
    """SendInput でスキャンコード＋VK を同時指定してキーを押す / 離す。"""
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
    """フックスレッドから委譲された update_forward() 呼び出しを処理するワーカー。"""
    while not _stop_event.is_set():
        try:
            _call_queue.get(timeout=0.1)
            update_forward()
        except queue.Empty:
            # タイムアウト: フォーカス変化の定期チェックも兼ねる
            update_forward()


def on_click(x, y, button, pressed) -> None:
    """WH_MOUSE_LL フックスレッドから呼ばれる。状態更新とキュー投入のみ。"""
    global _left_down, _right_down
    with _lock:
        if button == mouse.Button.left:
            _left_down = pressed
        elif button == mouse.Button.right:
            _right_down = pressed
    _call_queue.put_nowait(None)  # ワーカーに通知（SendInput はワーカー側で実行）


def _emergency_cleanup() -> None:
    _stop_event.set()
    with _lock:
        forwarding = _forwarding
    if forwarding:
        _stop_forward()


def _signal_handler(signum, frame) -> None:
    logger.info("SIGINT received, stopping...")
    _stop_event.set()


def main() -> None:
    atexit.register(_emergency_cleanup)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info(
        "vrchat-mouserun started (Ctrl+C to stop) INPUT size=%d",
        ctypes.sizeof(INPUT),
    )

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    with mouse.Listener(on_click=on_click) as listener:
        _stop_event.wait()
        listener.stop()

    logger.info("vrchat-mouserun stopped")


if __name__ == "__main__":
    main()
