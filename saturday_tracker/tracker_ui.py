#!/usr/bin/env python3
"""Сервер трекера: отдаёт собранный фронт и ЖИВЫЕ данные из файлов.

Запуск:
    python3 mastery/tools/tracker_ui.py          # http://127.0.0.1:8777

Фронт — `static/tracker_app.html`, самодостаточный бандл (React и mermaid
вшиты в него base64, сеть в рантайме не нужна). Он умеет открываться и
двойным кликом через `file://`, но тогда показывает СНАПШОТ данных, вшитый
в момент сборки. Здесь тот же бандл получает данные из живых файлов:
`window.__resources` он не найдёт, поэтому пойдёт по относительным путям
`data/*.csv` и `data/comments/<КЛЮЧ>.md` — их и отдаёт этот сервер.

Контракт с бандлом (снят чтением его кода, не выдуман):
    data/tasks.csv, data/goals.csv, data/links.csv, data/sprints.csv
    data/comments/<КЛЮЧ>.md   — формат «## автор · дата», как пишет store

Запись остаётся на стороне Python: бандл read-only (ни одного POST), а
`tracker_store` даёт лок, атомарную запись и git-коммит на каждую правку.
API записи ниже уже готов — фронт подключится к нему, когда научится писать.

Зависимостей нет, только stdlib. Слушает 127.0.0.1: наружу не выставляем.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__:
    from . import tracker_mermaid as mmd
    from . import tracker_store as store
    from . import tracker_write_patch as write_patch
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tracker_mermaid as mmd            # noqa: E402
    import tracker_store as store            # noqa: E402
    import tracker_write_patch as write_patch  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
APP = STATIC / "tracker_app.html"

# Порт, на котором слушаем: нужен для абсолютных URL внутри бандла.
PORT = 8777

# Что бандл просит по имени → какой файл трекера ему отдать.
CSV_ROUTES = {
    "tasks.csv": store.TASKS,
    "goals.csv": store.GOALS,
    "links.csv": store.LINKS,
    "sprints.csv": store.SPRINTS,
}


def app_html(deep_key: str | None = None) -> bytes:
    """Бандл, у которого отобран вшитый снапшот данных.

    Зачем. Бандл самодостаточен: в него base64-ом зашиты и React с mermaid,
    и СНИМОК данных на момент сборки. Загрузчик строит `window.__resources`
    из списка `ext_resources` и читает данные так:

        fetch(window.__resources[id] || 'data/tasks.csv')

    То есть пока в карте есть запись для `tasksCsv`, относительный путь не
    используется никогда — и сервер с живыми файлами стоит впустую. Проверено
    логом запросов: браузер дёргал только `/`, ни одного `/data/*.csv`.

    Поэтому на выдаче вырезаем из `ext_resources` записи данных (CSV и
    комментарии `cm_*`), оставляя вендорные. Тогда `||` срабатывает, и бандл
    идёт за живыми файлами к этому серверу.

    Файл на диске не трогаем: он должен продолжать открываться двойным кликом
    через file:// — там снапшот и есть единственный источник данных.
    """
    raw = APP.read_text(encoding="utf-8")
    m = re.search(r'(<script type="__bundler/ext_resources">)(.*?)(</script>)',
                  raw, re.S)
    if not m:
        # Формат бандла изменился — отдаём как есть. Хуже, чем живые данные,
        # но лучше, чем белый экран из-за нашей самодеятельности.
        return raw.encode("utf-8")

    try:
        entries = json.loads(m.group(2))
    except json.JSONDecodeError:
        return raw.encode("utf-8")

    kept = [e for e in entries
            if not (str(e.get("id", "")).endswith("Csv")
                    or str(e.get("id", "")).startswith("cm_"))]
    patched = (raw[:m.start(2)] + json.dumps(kept, ensure_ascii=False)
               + raw[m.end(2):])

    # Вырезать записи мало. Страница монтируется в iframe с blob:-URL, а у
    # blob-документа относительный `data/tasks.csv` резолвится к blob-
    # происхождению, а не к нашему серверу — запрос до сервера просто не
    # доходит (проверено логом: браузер дёргал только `/`). Поэтому подменяем
    # относительные пути на абсолютные адреса этого же сервера.
    origin = f"http://127.0.0.1:{PORT}"
    patched = patched.replace("'data/", f"'{origin}/data/")

    # Третьим шагом — включаем запись. Бандл собран read-only: шесть мест
    # с пометкой «мокап» показывают тост вместо сохранения. Патч заменяет их
    # вызовами API этого сервера (лок, атомарность, git-коммит в store).
    tm = re.search(r'(<script type="__bundler/template">)(.*?)(</script>)',
                   patched, re.S)
    if tm:
        try:
            tpl = json.loads(tm.group(2).strip())
            new_tpl, missed = write_patch.apply(tpl, origin, deep_key)
            if missed:
                # Не молчим: иначе кнопки останутся мокапами, а выглядеть
                # всё будет рабочим — худший из возможных исходов.
                print(f"ВНИМАНИЕ: патч записи не применился к: {', '.join(missed)}. "
                      f"Похоже, бандл пересобран и формулировки изменились — "
                      f"править mastery/tools/tracker_write_patch.py",
                      file=sys.stderr)
            # `</` обязано быть экранировано. Наш рантайм содержит
            # закрывающий тег `</script>`, а json.dumps его не трогает —
            # и HTML-парсер закрывает `<script type="__bundler/template">`
            # прямо посреди JSON-строки. Дальше загрузчик получает обрывок,
            # молча не подставляет blob-URL, и страница пытается грузить
            # скрипты по голым uuid: в логе 404 на `/%22<uuid>/%22`.
            # Так же поступает сам загрузчик бандла (`replace(/<\//g,'<\\/')`).
            encoded = json.dumps(new_tpl, ensure_ascii=False).replace("</", "<\\/")
            patched = patched[:tm.start(2)] + encoded + patched[tm.end(2):]
        except json.JSONDecodeError:
            print("ВНИМАНИЕ: template бандла не разобрался как JSON — "
                  "запись не подключена, интерфейс останется read-only",
                  file=sys.stderr)

    # Схлопывание вкладок вставляем во ВНЕШНЮЮ страницу, а не в шаблон:
    # приложение монтируется в blob:-документ с origin === "null", и
    # BroadcastChannel там изолирован — вкладки друг друга не видят
    # (проверено: канал молчал). Внешняя страница на настоящем origin.
    dedup = write_patch.TAB_DEDUP
    if deep_key:
        safe = json.dumps(deep_key, ensure_ascii=False)
        dedup = f"<script>window.__trkOpen = {safe};</script>" + dedup
    if "</head>" in patched:
        patched = patched.replace("</head>", dedup + "</head>", 1)

    return patched.encode("utf-8")

NOT_FOUND_PAGE = """<!doctype html><meta charset="utf-8">
<style>body{font:15px/1.6 -apple-system,sans-serif;background:#1e1b18;color:#e6edf3;
padding:40px;max-width:680px;margin:0 auto}code{background:#2d2a26;padding:2px 6px;
border-radius:4px}a{color:#d67f48}</style>
<h2>Фронт трекера не найден</h2>
<p>Ожидается файл <code>mastery/tools/static/tracker_app.html</code> — собранный
бандл интерфейса.</p>
<p>Данные при этом на месте и доступны как есть:
<a href="/data/tasks.csv">/data/tasks.csv</a>,
<a href="/api/tickets">/api/tickets</a>.</p>
""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "TrackerUI/3.0"

    def log_message(self, *a):
        pass

    # --- отправка ----------------------------------------------------------

    def _send(self, body: bytes, code: int = 200,
              ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # Бандл монтирует страницу в iframe с blob:-URL, а у такого документа
        # origin === "null". Без этого заголовка любой его fetch к нам
        # отбивается CORS-ом ДО запроса: в логе сервера пусто, в консоли
        # «Failed to fetch», и приложение молча показывает вшитый снапшот —
        # выглядит как «сервер не работает», хотя сервер здоров.
        # Воспроизведено отдельно: blob-iframe → fetch → ERR:Failed to fetch.
        # Слушаем только 127.0.0.1, поэтому «*» наружу ничего не открывает.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        # Данные меняются под руками (агент правит те же файлы), поэтому
        # кэшировать их нельзя: браузер показал бы вчерашний трекер.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), code,
                   "application/json; charset=utf-8")

    def _text_file(self, path: Path, ctype: str):
        if not path.exists():
            # 404 текстом, а не HTML: бандл ждёт text/csv и проверяет r.ok.
            self._send(b"", 404, ctype)
            return
        self._send(path.read_bytes(), 200, ctype)

    # --- GET ---------------------------------------------------------------

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)

        # Прямая ссылка на тикет: /t/DEMO-2.
        # Отдаём тот же фронт, но с указанием, какую карточку открыть.
        # Ссылка должна работать откуда угодно — из чата, из коммита, из
        # комментария, — поэтому это настоящий URL, а не якорь.
        deep_key = None
        if path.startswith("/t/"):
            deep_key = path[3:].strip("/")

        if (path in ("/", "/index.html") or path.startswith("/app")
                or deep_key is not None):
            if not APP.exists():
                self._send(NOT_FOUND_PAGE, 503)
                return
            self._send(app_html(deep_key))
            return

        # Живые CSV — то, ради чего сервер и нужен.
        if path.startswith("/data/comments/") and path.endswith(".md"):
            key = path[len("/data/comments/"):-3]
            self._text_file(store.COMMENTS / f"{key}.md",
                            "text/markdown; charset=utf-8")
            return

        if path.startswith("/data/"):
            name = path[len("/data/"):]
            target = CSV_ROUTES.get(name)
            if target is None:
                self._send(b"", 404, "text/plain; charset=utf-8")
                return
            self._text_file(target, "text/csv; charset=utf-8")
            return

        if path == "/static/mermaid.min.js":
            f = STATIC / "mermaid.min.js"
            if not f.exists():
                self._send(b"// mermaid not installed", 404,
                           "application/javascript")
                return
            self._send(f.read_bytes(), 200,
                       "application/javascript; charset=utf-8")
            return

        # --- служебное API (чтение) ---
        if path == "/api/tickets":
            self._json(store.load_tickets())
            return
        if path == "/api/links":
            self._json(store.load_links())
            return
        if path == "/api/sprints":
            self._json([{**s, **store.sprint_stats(s.get("ключ") or "")}
                        for s in store.load_sprints()])
            return
        if path.startswith("/api/comments/"):
            self._json(store.load_comments(path[len("/api/comments/"):]))
            return
        if path.startswith("/api/mermaid/"):
            which = path[len("/api/mermaid/"):]
            arg = urllib.parse.parse_qs(u.query).get("key", [None])[0]
            try:
                self._send(_mermaid(which, arg).encode("utf-8"), 200,
                           "text/plain; charset=utf-8")
            except KeyError:
                self._send(f"Неизвестный тип: {which}".encode("utf-8"), 404,
                           "text/plain; charset=utf-8")
            return

        self._send(b"", 404, "text/plain; charset=utf-8")

    # --- POST: запись ------------------------------------------------------

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype:
            return json.loads(raw or "{}")
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            data = self._body()
        except json.JSONDecodeError as exc:
            self._json({"ok": False, "error": f"битый JSON: {exc}"}, 400)
            return

        try:
            if u.path == "/api/update":
                key = data.pop("ключ", "")
                author = data.pop("автор", "") or store.DEFAULT_AUTHOR
                changes = {k: v for k, v in data.items() if k in store.EDITABLE}
                res = store.update_ticket(key, changes, author=author)
                self._json({"ok": True, **res})

            elif u.path == "/api/comment":
                res = store.add_comment(data.get("ключ", ""),
                                        data.get("текст", ""),
                                        author=data.get("автор") or store.DEFAULT_AUTHOR)
                self._json({"ok": True, **res})

            elif u.path == "/api/link/add":
                res = store.add_link(data.get("откуда", ""), data.get("тип", ""),
                                     (data.get("куда") or "").strip())
                self._json({"ok": True, **res})

            elif u.path == "/api/link/remove":
                res = store.remove_link(data.get("откуда", ""),
                                        data.get("тип", ""), data.get("куда", ""))
                if not res["удалено"]:
                    # С карточки видна и обратная сторона, а в файле ребро
                    # лежит в активной форме — пробуем перевёрнутое.
                    inv = {"блокируется": "блокирует"}.get(data.get("тип"),
                                                           data.get("тип"))
                    res = store.remove_link(data.get("куда", ""), inv,
                                            data.get("откуда", ""))
                self._json({"ok": True, **res})

            elif u.path == "/api/create":
                res = store.create_ticket(
                    data.get("направление", ""),
                    {k: data.get(k, "") for k in store.EDITABLE},
                    author=data.get("автор") or store.DEFAULT_AUTHOR)
                self._json({"ok": True, **res})

            else:
                self._json({"ok": False, "error": "нет такого действия"}, 404)

        except store.TrackerError as exc:
            # Пользовательская ошибка: показать человеку, не трейсбеком.
            self._json({"ok": False, "error": str(exc)}, 409)


def _mermaid(which: str, key: str | None) -> str:
    table = {
        "graph": lambda: mmd.graph(),
        "kanban": lambda: mmd.kanban(),
        "gantt": lambda: mmd.gantt(),
        "treeview": lambda: mmd.treeview(),
        "sankey": lambda: mmd.sankey(),
        "pie": lambda: mmd.pie(),
        "mindmap": lambda: mmd.mindmap(key or "DEMO-1"),
        "ishikawa": lambda: mmd.ishikawa(key or "DEMO-1"),
    }
    return table[which]()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сервер трекера: собранный фронт + живые данные из файлов")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--data", metavar="DIR",
                    help="каталог с tasks.csv/goals.csv/links.csv "
                         "(по умолчанию: $SATURDAY_TRACKER_DATA, затем ./tracker, "
                         "затем демо-данные examples/tracker)")
    args = ap.parse_args()

    global PORT
    PORT = args.port

    if args.data:
        path = Path(args.data).expanduser()
        if not path.is_dir():
            print(f"Каталога данных нет: {path}", file=sys.stderr)
            return 1
        store.set_data_dir(path)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno == 48:
            print(f"Порт {args.port} занят — вероятно, трекер уже запущен.\n"
                  f"Откройте http://127.0.0.1:{args.port} или другой порт:\n"
                  f"    saturday-tracker --port {args.port + 1}",
                  file=sys.stderr)
            return 1
        raise

    tickets = len(store.load_tickets())
    print(f"Трекер:  http://127.0.0.1:{args.port}")
    print(f"Фронт:   {'static/tracker_app.html' if APP.exists() else 'НЕ НАЙДЕН'}")
    print(f"Данные:  {store.TRACKER} ({tickets} тикетов, живые файлы)")
    print("Ctrl+C — остановить")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
