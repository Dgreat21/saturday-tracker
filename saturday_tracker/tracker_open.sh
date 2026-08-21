#!/bin/bash
# Открыть трекер в СОБСТВЕННОМ окне (и поднять сервер, если он не запущен).
#
#   ./tracker_open.sh                 # список задач
#   ./tracker_open.sh DEMO-2    # сразу карточка тикета
#
# Почему отдельное приложение-окно, а не вкладка браузера.
# Системная настройка `AppleWindowTabbingMode = always` (её ставили, чтобы
# ссылки из чата не плодили окна) делает ровно обратное для трекера: он
# уезжает вкладкой в общее окно к остальным двум десяткам. Трекер — рабочий
# инструмент, ему нужно своё окно, которое не теряется среди вкладок.
#
# Chrome умеет `--app=URL`: окно без адресной строки и вкладок, отдельная
# иконка в Dock, переключение по Cmd+Tab. Safari такого режима не имеет,
# поэтому здесь именно Chrome — при том что браузер по умолчанию Safari,
# и ссылки из чата по-прежнему открываются в нём.
#
# `--user-data-dir` отдельный: у окна свой профиль, поэтому оно не зависит
# от вкладок и сессий основного Chrome и не мешает им.

set -euo pipefail

PORT="${TRACKER_PORT:-8777}"
KEY="${1:-}"
CTX="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROFILE="$HOME/.dsh-agent/chrome-tracker"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

url="http://127.0.0.1:$PORT/"
[ -n "$KEY" ] && url="${url}t/${KEY}"

# 1. Сервер. Поднимаем молча, если его нет: открывать окно с ошибкой
#    подключения бессмысленно.
if ! curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" 2>/dev/null; then
    echo "Сервер трекера не отвечает — запускаю…"
    (cd "$CTX" && nohup python3 mastery/tools/tracker_ui.py --port "$PORT" \
        > "$HOME/.dsh-agent/logs/tracker_ui.log" 2>&1 &)
    for _ in $(seq 1 25); do
        curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" 2>/dev/null && break
        sleep 0.4
    done
    if ! curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" 2>/dev/null; then
        echo "Не поднялся. Смотрите ~/.dsh-agent/logs/tracker_ui.log" >&2
        exit 1
    fi
fi

if [ ! -x "$CHROME" ]; then
    echo "Chrome не найден: $CHROME" >&2
    echo "Открываю в браузере по умолчанию — он положит трекер вкладкой." >&2
    open "$url"
    exit 0
fi

# 2. Окно. Если оно уже открыто, второй --app создаст ЕЩЁ одно, поэтому
#    сначала пробуем переиспользовать существующее через AppleScript.
reused=$(osascript <<AS 2>/dev/null || true
tell application "System Events"
  if not (exists process "Google Chrome") then return "no"
end tell
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t contains "127.0.0.1:$PORT" then
        set URL of t to "$url"
        set index of w to 1
        activate
        return "yes"
      end if
    end repeat
  end repeat
end tell
return "no"
AS
)

if [ "$reused" = "yes" ]; then
    echo "Трекер уже открыт — переключил на $url"
    exit 0
fi

mkdir -p "$PROFILE"
"$CHROME" --app="$url" --user-data-dir="$PROFILE" \
    --window-size=1500,950 > /dev/null 2>&1 &
echo "Открыт: $url"
