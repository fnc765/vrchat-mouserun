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
import sys
import threading
import time
from pathlib import Path
from pynput import mouse

# ── ログ設定 ──────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _log_dir = Path(os.environ.get("APPDATA", Path.home())) / "vrchat-mouserun"
else:
    _log_dir = Path(__file__).parent

_log_dir.mkdir(parents=True, exist_ok=True)
log_path = _log_dir / "vrchat-mouserun.log"

logging.basicConfig(
    filename=str(log_path),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Win32 定数 ────────────────────────────────────────────
VK_W = 0x57
VK_LSHIFT = 0xA0
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

# ── ctypes 構造体 ─────────────────────────────────────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

# Union には ki のみ（mi/ui は使用しない）
class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]

user32 = ctypes.windll.user32

# ── 状態管理 ──────────────────────────────────────────────
_lock = threading.Lock()
_left_down = False
_right_down = False
_forwarding = False  # 現在 W+Shift を送信中かどうか
_running = True      # バックグラウンドスレッド制御用


def is_vrchat_active() -> bool:
    """フォアグラウンドウィンドウのタイトルが 'VRChat' か確認（完全一致）。"""
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip() == "VRChat"


def send_key(vk: int, down: bool) -> None:
    """SendInput で指定仮想キーを押す / 離す。"""
    flags = 0 if down else KEYEVENTF_KEYUP
    inp = INPUT(
        type=INPUT_KEYBOARD,
        _input=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
        ),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _start_forward() -> None:
    """W+LShift のキーダウンを送信する（TOCTOU対策として直前に再確認）。"""
    if not is_vrchat_active():
        logger.warning("focus changed before SendInput, aborting start")
        return
    logger.info("forward start")
    send_key(VK_LSHIFT, True)
    send_key(VK_W, True)


def _stop_forward() -> None:
    """W+LShift のキーアップを送信する。"""
    logger.info("forward stop")
    send_key(VK_W, False)
    send_key(VK_LSHIFT, False)


def update_forward() -> None:
    """_left_down / _right_down の状態を見て W+Shift の ON/OFF を切り替える。ロック外で呼ぶこと。"""
    global _forwarding
    with _lock:
        left, right = _left_down, _right_down
        forwarding = _forwarding

    should_forward = left and right and is_vrchat_active()

    if should_forward and not forwarding:
        with _lock:
            _forwarding = True
        _start_forward()
    elif not should_forward and forwarding:
        with _lock:
            _forwarding = False
        _stop_forward()


def _focus_watcher() -> None:
    """100ms ごとにフォーカス状態を確認し、VRChat が非アクティブになったら W+Shift を解除する。"""
    while _running:
        update_forward()
        time.sleep(0.1)


def on_click(x, y, button, pressed) -> None:
    global _left_down, _right_down
    # ロック内は状態変数の読み書きのみ
    with _lock:
        if button == mouse.Button.left:
            _left_down = pressed
        elif button == mouse.Button.right:
            _right_down = pressed
    # Win32 呼び出しはロック外で実行
    update_forward()


def _emergency_cleanup() -> None:
    """atexit ハンドラ: 終了時にキーが押しっぱなしにならないよう解放する。"""
    global _running
    _running = False
    with _lock:
        forwarding = _forwarding
    if forwarding:
        _stop_forward()


def main() -> None:
    atexit.register(_emergency_cleanup)
    logger.info("vrchat-mouserun started")

    # バックグラウンドスレッドでフォーカス変化を定期監視
    watcher = threading.Thread(target=_focus_watcher, daemon=True)
    watcher.start()

    with mouse.Listener(on_click=on_click) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            pass

    logger.info("vrchat-mouserun stopped")


if __name__ == "__main__":
    main()
