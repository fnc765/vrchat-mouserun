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
import signal
import sys
import threading
import time
from pathlib import Path
from pynput import mouse

# ── ログ設定 ──────────────────────────────────────────────
def _setup_logging() -> None:
    if getattr(sys, "frozen", False):
        # PyInstaller exe 実行時: %APPDATA% 配下のファイルへ出力
        log_dir = Path(os.environ.get("APPDATA", Path.home())) / "vrchat-mouserun"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_dir / "vrchat-mouserun.log"), encoding="utf-8")
    else:
        # Python 直接実行時: コンソールへ出力
        handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler],
    )

_setup_logging()
logger = logging.getLogger(__name__)

# ── Win32 定数 ────────────────────────────────────────────
VK_W = 0x57
VK_LSHIFT = 0xA0
KEYEVENTF_KEYUP     = 0x0002
KEYEVENTF_SCANCODE  = 0x0008  # wScan を使うことを Windows に明示
INPUT_KEYBOARD = 1

# ハードウェアスキャンコード（MapVirtualKeyW(vk, 0) の値）
# Unity Input System は WM_INPUT (RawInput) 経由でスキャンコードを識別するため必須
SC_W      = 0x11
SC_LSHIFT = 0x2A

# ── ctypes 構造体 ─────────────────────────────────────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),   # ULONG_PTR: x86=4B, x64=8B
    ]

# Union は MOUSEINPUT(32B) / KEYBDINPUT(24B) / HARDWAREINPUT(8B) を包む。
# KEYBDINPUT(24B) 単独だと Union=24B になるため _pad で 32B に揃える。
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
_lock = threading.Lock()
_left_down = False
_right_down = False
_forwarding = False  # 現在 W+Shift を送信中かどうか
_stop_event  = threading.Event()  # 終了シグナル
_click_event = threading.Event()  # on_click → _focus_watcher への通知


def is_vrchat_active() -> bool:
    """フォアグラウンドウィンドウのタイトルが 'VRChat' か確認（完全一致）。"""
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip() == "VRChat"


def send_key(vk: int, scan: int, down: bool) -> None:
    """SendInput で指定仮想キーを押す / 離す。

    wVk と wScan を両方設定し KEYEVENTF_SCANCODE を立てることで、
    Unity Input System (RawInput 経由) にも正常に届くようにする。
    wScan=0 のまま仮想キーのみ送ると WM_INPUT の scan field が 0x00 になり
    VRChat に無視される。
    """
    flags = KEYEVENTF_SCANCODE
    if not down:
        flags |= KEYEVENTF_KEYUP
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
    """W+LShift のキーダウンを送信する（TOCTOU対策として直前に再確認）。"""
    if not is_vrchat_active():
        logger.warning("focus changed before SendInput, aborting start")
        return
    logger.info("forward start")
    send_key(VK_LSHIFT, SC_LSHIFT, True)
    send_key(VK_W,      SC_W,      True)


def _stop_forward() -> None:
    """W+LShift のキーアップを送信する。"""
    logger.info("forward stop")
    send_key(VK_W,      SC_W,      False)
    send_key(VK_LSHIFT, SC_LSHIFT, False)


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
    """クリックイベント通知または 100ms タイムアウトで update_forward() を呼ぶ。

    SendInput は必ずこのスレッドから呼ぶ。
    pynput の WH_MOUSE_LL フックコールバック (on_click) 内から SendInput を
    呼ぶと、Windows がフックスレッドをブロックする可能性があるため、
    on_click は状態更新 + _click_event.set() のみ行う。
    """
    while not _stop_event.is_set():
        _click_event.wait(timeout=0.1)
        _click_event.clear()
        update_forward()


def on_click(x, y, button, pressed) -> None:
    global _left_down, _right_down
    # ロック内は状態変数の読み書きのみ
    with _lock:
        if button == mouse.Button.left:
            _left_down = pressed
        elif button == mouse.Button.right:
            _right_down = pressed
    # SendInput は WH_MOUSE_LL フックスレッド内では呼ばない。
    # _focus_watcher スレッドに通知して処理を委ねる。
    _click_event.set()


def _emergency_cleanup() -> None:
    """atexit ハンドラ: 終了時にキーが押しっぱなしにならないよう解放する。"""
    _stop_event.set()
    with _lock:
        forwarding = _forwarding
    if forwarding:
        _stop_forward()


def _signal_handler(signum, frame) -> None:
    """Ctrl+C (SIGINT) を受け取って終了イベントをセットする。"""
    logger.info("SIGINT received, stopping...")
    _stop_event.set()


def main() -> None:
    atexit.register(_emergency_cleanup)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info(
        "vrchat-mouserun started (Ctrl+C to stop) "
        "INPUT size=%d KEYBDINPUT size=%d",
        ctypes.sizeof(INPUT),
        ctypes.sizeof(KEYBDINPUT),
    )

    # バックグラウンドスレッドでフォーカス変化を定期監視
    watcher = threading.Thread(target=_focus_watcher, daemon=True)
    watcher.start()

    with mouse.Listener(on_click=on_click) as listener:
        _stop_event.wait()
        listener.stop()

    logger.info("vrchat-mouserun stopped")


if __name__ == "__main__":
    main()
