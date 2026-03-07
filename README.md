# vrchat-mouserun

VRChatがアクティブウィンドウのとき、マウスの左右ボタン同時押しで **W + 左Shift** を送信するWindows常駐ツールです。VRChatで走りながら前進する操作に使います。

## 機能

- マウス左右ボタンの同時押しを検出し、VRChatへ W + LShift キー入力を送信
- タスクトレイに常駐（アイコン右クリックでメニュー表示）
- Windows起動時の自動起動をトレイメニューから切り替え可能
- EXE起動時はログを `%APPDATA%\vrchat-mouserun\vrchat-mouserun.log` へ保存

## 動作環境

- Windows 10 / 11
- Python 3.11 以上（スクリプト実行の場合）

## インストール

### EXE を使う場合（推奨）

`dist/vrchat-mouserun.exe` をダウンロードしてそのまま実行してください。インストール不要のポータブル単体EXEです。

### Python スクリプトとして実行する場合

[uv](https://docs.astral.sh/uv/) が必要です。

```
uv sync
```

## 使い方

### EXE を起動する場合

```
dist\vrchat-mouserun.exe
```

### Python スクリプトとして実行する場合

```
uv run python main.py
```

起動するとタスクトレイにアイコンが表示されます。VRChatを前面にした状態でマウスの左右ボタンを同時押しすると、W + LShift キーが送信されます。

終了するにはタスクトレイアイコンを右クリックし、**Exit** を選択してください。

## タスクトレイ

タスクトレイアイコンを右クリックすると以下のメニューが表示されます。

| メニュー項目 | 説明 |
|---|---|
| Windows起動時に自動起動 | チェックを入れるとWindowsログイン時に自動起動します |
| Exit | アプリを終了します |

## ビルド方法

[uv](https://docs.astral.sh/uv/) と PyInstaller を使ってポータブルEXEを生成します。

```
uv run pyinstaller main.py --onefile --noconsole --name vrchat-mouserun --hidden-import pynput.mouse._win32 --hidden-import pynput.keyboard._win32
```

VS Code を使っている場合は `Ctrl+Shift+B`（**Build exe (PyInstaller)**）でも実行できます。

ビルド成功後、`dist/vrchat-mouserun.exe` が生成されます。
