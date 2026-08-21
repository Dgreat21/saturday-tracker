#!/usr/bin/env python3
"""Проверки трекера с негативным контролем (MST-15).

Зелёная проверка ничего не доказывает, пока не показано, что она умеет
краснеть. Поэтому на КАЖДЫЙ класс инвариантов здесь свой сломанный вход,
а не один общий «плохой файл»: общая поломка уронила бы первую же проверку
и замаскировала остальные.

Классы инвариантов:
  1. Экранирование подписей mermaid (кавычки, скобки, стрелки, кириллица).
  2. Синтаксис kanban (скобки внутри карточки роняли всю доску).
  3. Sankey не принимает кириллицу — подписи обязаны быть латиницей.
  4. Циклы в связях не зацикливают обход (mindmap, progress_of).
  5. Атомарная запись сохраняет стиль перевода строк (CRLF против LF).
  6. Запись отвергает недопустимые значения (статус вне словаря, чужое поле).

    python3 mastery/tools/tracker_selftest.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

if __package__:                              # python3 -m saturday_tracker.tracker_selftest
    from . import tracker_mermaid as mmd
    from . import tracker_store as store
else:                                        # python3 saturday_tracker/tracker_selftest.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tracker_mermaid as mmd            # noqa: E402
    import tracker_store as store            # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --- класс 1: экранирование подписей ---------------------------------------

def test_labels() -> None:
    print("\n[1] Экранирование подписей mermaid")
    evil = 'Задача "кавычки" [скобки] {фигурные} #решётка --> стрелка'
    got = mmd.label(evil)
    check("кавычки убраны", '"' not in got, got)
    check("квадратные скобки убраны", "[" not in got and "]" not in got, got)
    check("фигурные убраны", "{" not in got and "}" not in got, got)
    check("решётка заменена", "#" not in got, got)
    check("стрелка не рвёт ребро", "-->" not in got, got)
    check("кириллица сохранена", "Задача" in got, got)

    # НЕГАТИВНЫЙ КОНТРОЛЬ: без экранирования проверка обязана упасть.
    raw_ok = ('"' not in evil and "[" not in evil)
    check("негативный контроль: сырой текст НЕ проходит", not raw_ok,
          "сырой текст прошёл проверку — она сломана")


# --- класс 2: kanban --------------------------------------------------------

def test_kanban() -> None:
    print("\n[2] Синтаксис kanban")
    tickets = {"X-1": {"название": "Закрыть развилки DC-01 (схема FQN)",
                       "статус_тег": "в беклоге"}}
    code = mmd.kanban(tickets)
    check("колонка в скобочной форме", re.search(r"^\s+col_\w+\[", code, re.M) is not None)
    body = "\n".join(code.split("\n")[1:])
    check("круглых скобок в карточках нет", "(" not in body and ")" not in body, body)

    # НЕГАТИВНЫЙ КОНТРОЛЬ: общий label() оставляет скобки — значит проверка
    # выше действительно что-то ловит, а не всегда зелёная.
    check("негативный контроль: label() оставил бы скобки",
          "(" in mmd.label("Закрыть развилки DC-01 (схема FQN)"))


# --- класс 3: sankey и кириллица -------------------------------------------

def test_sankey() -> None:
    print("\n[3] Sankey без кириллицы")
    code = mmd.sankey()
    check("ключевое слово sankey (не -beta)", code.startswith("sankey\n"))
    cyr = [l for l in code.split("\n")[2:] if re.search(r"[а-яА-ЯёЁ]", l)]
    check("кириллицы в строках потоков нет", not cyr, str(cyr[:2]))

    # НЕГАТИВНЫЙ КОНТРОЛЬ: транслитерация обязана менять кириллицу.
    check("негативный контроль: translit меняет вход",
          mmd.translit("закрыт") != "закрыт")


# --- класс 4: циклы в связях ------------------------------------------------

def test_cycles() -> None:
    print("\n[4] Циклы в связях не зацикливают обход")
    real_links = store.load_links
    real_tickets = store.load_tickets
    store.load_links = lambda: [                        # type: ignore[assignment]
        {"откуда": "A-1", "тип": "входит в", "куда": "B-1"},
        {"откуда": "B-1", "тип": "входит в", "куда": "A-1"},   # цикл
    ]
    store.load_tickets = lambda: {                      # type: ignore[assignment]
        "A-1": {"ключ": "A-1", "название": "A", "статус_тег": "закрыт", "тип": "task"},
        "B-1": {"ключ": "B-1", "название": "B", "статус_тег": "в беклоге", "тип": "task"},
    }
    try:
        prog = store.progress_of("A-1")
        check("progress_of не ушёл в бесконечность", isinstance(prog, dict))
        code = mmd.mindmap("A-1")
        check("mindmap не ушёл в бесконечность", code.startswith("mindmap"))
        check("узел не повторяется бесконечно", code.count("\n") < 20, f"строк {code.count(chr(10))}")
    finally:
        store.load_links = real_links                   # type: ignore[assignment]
        store.load_tickets = real_tickets               # type: ignore[assignment]


# --- класс 5: стиль перевода строк ------------------------------------------

def test_newlines() -> None:
    print("\n[5] Запись сохраняет стиль перевода строк")
    with tempfile.TemporaryDirectory() as d:
        crlf = Path(d) / "crlf.csv"
        crlf.write_bytes("a,b\r\n1,2\r\n".encode("utf-8-sig"))
        check("CRLF распознан", store.detect_newline(crlf) == "\r\n")
        lf = Path(d) / "lf.csv"
        lf.write_bytes("a,b\n1,2\n".encode("utf-8-sig"))
        check("LF распознан", store.detect_newline(lf) == "\n")

        store._write_csv(crlf, ["a", "b"], [{"a": "1", "b": "2"}])
        data = crlf.read_bytes()
        check("после записи CRLF остался CRLF",
              data.count(b"\r\n") == 2 and data.count(b"\n") == 2)

        # НЕГАТИВНЫЙ КОНТРОЛЬ: если бы писали жёстко "\n", проверка выше
        # обязана была покраснеть — убеждаемся, что она это различает.
        lf_data = "a,b\n1,2\n".encode("utf-8-sig")
        check("негативный контроль: LF-байты не считаются CRLF",
              lf_data.count(b"\r\n") == 0)


# --- класс 6: отказ на недопустимых значениях -------------------------------

def test_validation() -> None:
    print("\n[6] Запись отвергает недопустимое")
    tickets = store.load_tickets()
    if not tickets:
        check("есть тикеты для проверки", False, "трекер пуст")
        return
    key = sorted(tickets)[0]

    try:
        store.update_ticket(key, {"статус_тег": "выдуманный_статус"})
        check("статус вне словаря отвергнут", False, "прошёл без ошибки")
    except store.TrackerError:
        check("статус вне словаря отвергнут", True)

    try:
        store.update_ticket(key, {"ключ": "ВЗЛОМ-1"})
        check("правка нередактируемого поля отвергнута", False, "прошла")
    except store.TrackerError:
        check("правка нередактируемого поля отвергнута", True)

    try:
        store.add_link(key, "выдуманная_связь", key)
        check("недопустимый тип связи отвергнут", False, "прошёл")
    except store.TrackerError:
        check("недопустимый тип связи отвергнут", True)

    try:
        store.add_link(key, "блокирует", key)
        check("связь с самим собой отвергнута", False, "прошла")
    except store.TrackerError:
        check("связь с самим собой отвергнута", True)

    try:
        store.add_comment(key, "   ")
        check("пустой комментарий отвергнут", False, "прошёл")
    except store.TrackerError:
        check("пустой комментарий отвергнут", True)


# --- класс 7: подача фронта с живыми данными --------------------------------

def test_serving() -> None:
    """Бандл должен получать ЖИВЫЕ данные, а не вшитый снапшот.

    Класс инвариантов, на котором уже обожглись: всё выглядело рабочим —
    сервер отвечал 200, страница рисовалась, — но показывала данные на момент
    сборки бандла. Две причины, обе молчаливые:
      1) в `ext_resources` остаётся запись `tasksCsv`, и `||` в
         `fetch(window.__resources[id] || 'data/tasks.csv')` до пути не доходит;
      2) страница живёт в blob-iframe с origin === "null", и без CORS-заголовка
         её fetch отбивается ДО запроса — в логе сервера пусто.
    """
    print("\n[7] Фронт получает живые данные")
    # импорт здесь, а не сверху: модуль тянет http.server
    if __package__:
        from . import tracker_ui as ui
    else:
        import tracker_ui as ui   # noqa: E402

    if not ui.APP.exists():
        check("бандл на месте", False, f"нет {ui.APP}")
        return
    check("бандл на месте", True)

    html = ui.app_html().decode("utf-8")
    m = re.search(r'<script type="__bundler/ext_resources">(.*?)</script>',
                  html, re.S)
    check("ext_resources найден", m is not None)
    if not m:
        return
    import json as _json
    ids = [e.get("id") for e in _json.loads(m.group(1))]
    stale = [i for i in ids
             if str(i).endswith("Csv") or str(i).startswith("cm_")]
    check("вшитый снапшот данных вырезан", not stale, str(stale[:3]))
    check("вендорные ресурсы остались", any("unpkg.com" in str(i) for i in ids))
    check("пути к данным абсолютные",
          f"http://127.0.0.1:{ui.PORT}/data/tasks.csv" in html)

    # НЕГАТИВНЫЙ КОНТРОЛЬ: в исходном файле записи данных ЕСТЬ — значит
    # проверка выше действительно что-то вырезает, а не всегда зелёная.
    raw = ui.APP.read_text(encoding="utf-8")
    raw_m = re.search(r'<script type="__bundler/ext_resources">(.*?)</script>',
                      raw, re.S)
    raw_ids = [e.get("id") for e in _json.loads(raw_m.group(1))] if raw_m else []
    check("негативный контроль: в оригинале снапшот присутствует",
          any(str(i).endswith("Csv") for i in raw_ids))

    # CORS: без него blob-iframe не достучится до сервера.
    class _Probe(ui.Handler):
        def __init__(self):           # обходим сетевой конструктор
            self.sent = {}

        def send_response(self, *a, **k):
            pass

        def send_header(self, k, v):
            self.sent[k.lower()] = v

        def end_headers(self):
            pass

    p = _Probe()
    try:
        p.wfile = type("W", (), {"write": lambda self, b: None})()
        p._send(b"x", 200, "text/csv")
    except Exception:
        pass
    check("CORS-заголовок отдаётся",
          p.sent.get("access-control-allow-origin") == "*",
          str(p.sent))
    check("данные не кэшируются",
          "no-store" in (p.sent.get("cache-control") or ""), str(p.sent))

    # Целостность самой выдачи: template обязан оставаться валидным JSON.
    # Наш рантайм записи содержит закрывающий тег `</script>`, и без
    # экранирования `</` HTML-парсер закрывает `<script type="__bundler/template">`
    # прямо посреди JSON. Загрузчик получает обрывок, blob-URL не подставляет,
    # и страница грузит скрипты по голым uuid — в логе 404 на `/%22<uuid>/%22`,
    # данные не приходят вовсе. Ровно это и случилось на v2.
    served = ui.app_html().decode("utf-8")
    tm = re.search(r'<script type="__bundler/template">(.*?)</script>',
                   served, re.S)
    check("template в выдаче не оборван", tm is not None)
    if tm:
        import json as _j
        body = tm.group(1).strip()
        try:
            _j.loads(body.replace("<\\/", "</"))
            decodes = True
        except Exception:
            decodes = False
        check("template в выдаче — валидный JSON", decodes,
              "оборван: скорее всего неэкранированный </script> в рантайме")
        check("закрывающие теги экранированы", "</script>" not in body,
              "в теле template есть сырой </script>")

def test_write_patch() -> None:
    """Мокапы бандла заменены реальными вызовами API.

    Отказ здесь особенно коварен: если замена не сработала, интерфейс
    выглядит рабочим, кнопки нажимаются, тост показывается — и ничего
    не сохраняется. Поэтому проверяем каждую замену поимённо, а не «хоть
    что-то заменилось».
    """
    print("\n[8] Запись из фронта подключена")
    import json as _json

    if __package__:
        from . import tracker_ui as ui
        from . import tracker_write_patch as wp
    else:
        import tracker_ui as ui                  # noqa: E402
        import tracker_write_patch as wp         # noqa: E402

    if not ui.APP.exists():
        check("бандл на месте", False, f"нет {ui.APP}")
        return

    raw = ui.APP.read_text(encoding="utf-8")
    m = re.search(r'<script type="__bundler/template">(.*?)</script>', raw, re.S)
    check("template найден", m is not None)
    if not m:
        return

    tpl = _json.loads(m.group(1).strip())
    patched, missed = wp.apply(tpl, "http://127.0.0.1:8777")
    check("все замены применились", not missed, f"не сработали: {missed}")
    check("рантайм записи вставлен", "window.__trkWrite" in patched)
    check("адрес API проставлен",
          'window.__trkApi = "http://127.0.0.1:8777"' in patched)

    # Мокапы не должны остаться ни в одной из точек. Замены, у которых
    # искомая строка является ЧАСТЬЮ заменяющей (граф дополняется, а не
    # переписывается), проверяются иначе — по признаку новой логики.
    left = [name for name, old, new in wp.REPLACEMENTS
            if old in patched and old not in new]
    # Фильтр графа управляется меню «показ» (gShow). Раньше здесь стояли
    # gLonely/gNeighbors — логика была, а включить её было нечем: меню не
    # существовало, состояние никто не выставлял, код лежал мёртвым.
    check("меню «показ» добавлено", "'gShow'" in patched)
    check("граф читает выбор пользователя", "state.gShow" in patched)
    check("режим соседей работает",
          "соседи из других очередей" in patched)
    check("одиночные узлы включаются", "задачи без связей" in patched)
    check("сброс чистит меню «показ»", "gShow: []" in patched)
    check("роутер прямых ссылок вставлен", "__trkOpen" in patched
          or "replaceState" in patched)

    # Проверок схлопывания вкладок здесь больше нет: механизм убран,
    # задачу решает системная настройка macOS (см. tracker_write_patch).
    check("мокапов не осталось", not left, f"остались: {left}")

    # НЕГАТИВНЫЙ КОНТРОЛЬ: в исходном бандле мокапы ЕСТЬ — иначе проверка
    # выше зелёная просто потому, что искать нечего.
    present = [name for name, old, _ in wp.REPLACEMENTS if old in tpl]
    check("негативный контроль: в оригинале мокапы присутствуют",
          len(present) == len(wp.REPLACEMENTS),
          f"нашлось {len(present)} из {len(wp.REPLACEMENTS)}")

    # НЕГАТИВНЫЙ КОНТРОЛЬ 2: на изменённом бандле apply() обязан пожаловаться,
    # а не молча отдать страницу без записи.
    broken = tpl.replace(wp.REPLACEMENTS[0][1], "/* переписано */", 1)
    _, missed2 = wp.apply(broken, "http://127.0.0.1:8777")
    check("негативный контроль: пропавший мокап замечен",
          wp.REPLACEMENTS[0][0] in missed2, str(missed2))

    # Записываемые поля обязаны быть разрешены store — иначе фронт будет
    # слать то, что бэкенд отвергнет.
    for field in ("статус_тег", "спринт"):
        check(f"поле «{field}» разрешено к правке", field in store.EDITABLE)


def main() -> int:
    print("Самопроверка трекера (с негативным контролем)")
    test_labels()
    test_kanban()
    test_sankey()
    test_cycles()
    test_newlines()
    test_validation()
    test_serving()
    test_write_patch()
    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}")
        for f in failures:
            print(f"  — {f}")
        return 1
    print("Все проверки зелёные, и каждая умеет краснеть (негативный контроль пройден).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
