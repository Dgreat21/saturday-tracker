#!/usr/bin/env python3
"""Генерация mermaid-диаграмм ИЗ ДАННЫХ трекера.

Принцип: диаграмма не рисуется руками, а выводится из `tasks.csv`, `links.csv`
и `sprints.csv`. Нарисованная руками картинка протухает на второй правке
статуса и начинает врать; сгенерированная — не может разойтись с данными
по построению.

Синтаксис сверялся с reference-файлами скилла
`mastery/tools/addons/source/WH-2099/mermaid-skill` (37 файлов, по одному
на тип). Проверено рендером mermaid 11.17: все типы ниже отрисовываются.

Экранирование — главная ловушка. Названия задач содержат кавычки, скобки,
`-->`, `#`, кириллицу. Незаэкранированный текст ломает не одну подпись,
а весь граф целиком, поэтому `label()` применяется ко всему, что приходит
из данных.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__:
    from . import tracker_store as store
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tracker_store as store            # noqa: E402

# Цвет узла по статусу. Держим отдельно от CSS темы: mermaid рисует SVG
# своими средствами и переменные CSS не видит.
STATUS_FILL = {
    "в беклоге":     "#3d444d",
    "анализ":        "#1f6feb",
    "планирование":  "#9e6a03",
    "разработка":    "#1a7f37",
    "тестирование":  "#8250df",
    "ревью":         "#bc4c00",
    "закрыт":        "#21262d",
}


def label(text: str, limit: int = 46) -> str:
    """Текст из данных → безопасная подпись узла mermaid.

    Кавычки ломают строковый литерал; квадратные и круглые скобки —
    границы узла; `#` начинает entity; стрелки рвут ребро пополам.
    Заменяем, а не удаляем: подпись должна остаться читаемой.
    """
    t = str(text or "").strip()
    t = t.replace("\n", " ").replace("\r", " ")
    t = t.replace('"', "'")
    t = t.replace("[", "(").replace("]", ")")
    t = t.replace("{", "(").replace("}", ")")
    t = t.replace("#", "№")
    t = t.replace("-->", "→").replace("--", "—")
    t = re.sub(r"\s+", " ", t)
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def node_id(key: str) -> str:
    """Ключ тикета → идентификатор узла.

    `DEMO-1` содержит дефис, который mermaid в id не любит.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


# --- flowchart: граф связей ------------------------------------------------

def graph(keys: list[str] | None = None, *, direction: str = "TD") -> str:
    """Граф задач по links.csv.

    Рисуем ТОЛЬКО прямые рёбра из файла: обратные там не хранятся, и
    добавлять их сюда значило бы удвоить каждую стрелку.
    """
    tickets = store.load_tickets()
    links = store.load_links()
    if keys:
        want = set(keys)
        links = [e for e in links
                 if e.get("откуда") in want and e.get("куда") in want]
        nodes = {k: tickets[k] for k in want if k in tickets}
    else:
        nodes = tickets

    if not nodes:
        return "flowchart TD\n  empty[Нет задач под фильтр]"

    out = [f"flowchart {direction}"]
    for key, t in sorted(nodes.items()):
        nid = node_id(key)
        out.append(f'  {nid}["{label(key)}<br/>{label(t.get("название"), 38)}"]')
    for e in links:
        src, dst = e.get("откуда"), e.get("куда")
        if src not in nodes or dst not in nodes:
            continue
        arrow = {"блокирует": "-->", "входит в": "-.->", "реализует": "-.->",
                 "относится к": "---", "дублирует": "-.-"}.get(e.get("тип"), "-->")
        out.append(f'  {node_id(src)} {arrow}|{label(e.get("тип"), 14)}| {node_id(dst)}')
    for key, t in sorted(nodes.items()):
        fill = STATUS_FILL.get(t.get("статус_тег"), "#3d444d")
        out.append(f"  style {node_id(key)} fill:{fill},stroke:#6e7681,color:#e6edf3")
    for key in sorted(nodes):
        out.append(f'  click {node_id(key)} "/t/{key}"')
    return "\n".join(out)


# --- mindmap: эпик ---------------------------------------------------------

def mindmap(root: str, depth: int = 3) -> str:
    """Дерево «что входит в этот тикет». Ваш кейс для эпиков.

    Отступы в mindmap значимы — это и есть структура, поэтому собираем
    строки вручную, а не через join вложенных списков.
    """
    tickets = store.load_tickets()
    if root not in tickets:
        return f"mindmap\n  root(({label(root)}))\n    нет_такого_тикета"

    out = ["mindmap", f'  root(({label(tickets[root].get("название") or root, 34)}))']
    seen = {root}

    def walk(key: str, level: int):
        if level > depth:
            return
        for child in store.children_of(key):
            if child in seen:      # цикл в связях не должен зациклить обход
                continue
            seen.add(child)
            t = tickets.get(child, {})
            mark = "✓ " if t.get("статус_тег") in store.DONE_TAGS else ""
            out.append("  " * (level + 1) + f'{mark}{label(t.get("название") or child, 34)}')
            walk(child, level + 1)

    walk(root, 1)
    if len(out) == 2:
        out.append("    нет_вложенных_задач")
    return "\n".join(out)


# --- kanban ----------------------------------------------------------------

def kanban_label(text: str, limit: int = 40) -> str:
    """Подпись для kanban: там строже, чем в flowchart.

    Парсер kanban спотыкается на круглых скобках внутри `id[...]` — а общий
    `label()` как раз превращает квадратные скобки в круглые. Поймано
    рендером: «Закрыть развилки DC-01 (схема FQN)» роняла всю доску целиком,
    а не одну карточку. Поэтому здесь скобки убираются, а не заменяются.
    """
    t = label(text, limit)
    return t.replace("(", "").replace(")", "").replace(",", " ")


def kanban(tickets: dict[str, dict] | None = None) -> str:
    """Нативная kanban-диаграмма mermaid — та, что вы нашли."""
    tickets = tickets if tickets is not None else store.load_tickets()
    out = ["kanban"]
    for tag in store.STATUS_TAGS:
        items = [(k, t) for k, t in sorted(tickets.items())
                 if t.get("статус_тег") == tag]
        if not items:
            continue
        # Колонка — тоже узел `id[Заголовок]`, а не голый текст: без скобок
        # парсер спотыкается на первой же задаче со скобкой в названии.
        out.append(f"  col_{node_id(tag)}[{kanban_label(tag, 24)}]")
        for key, t in items[:20]:
            out.append(f'    {node_id(key)}[{kanban_label(t.get("название"), 40)}]')
    return "\n".join(out) if len(out) > 1 else "kanban\n  col_empty[Пусто]"


# --- gantt: спринты --------------------------------------------------------

def gantt() -> str:
    """Сроки по спринтам. Даты берём из sprints.csv, а не выдумываем."""
    sprints = store.load_sprints()
    if not sprints:
        return "gantt\n  title Спринтов пока нет\n  dateFormat YYYY-MM-DD\n  section —\n  нет данных :done, d1, 2026-01-01, 1d"

    out = ["gantt", "  title Спринты", "  dateFormat YYYY-MM-DD", "  axisFormat %d.%m"]
    for s in sprints:
        name = label(s.get("название") or s.get("ключ"), 30)
        start, end = (s.get("начало") or "").strip(), (s.get("конец") or "").strip()
        if not (start and end):
            continue
        out.append(f"  section {name}")
        st = store.sprint_stats(s.get("ключ") or "")
        tag = "done" if s.get("статус") == "завершён" else (
            "active" if s.get("статус") == "активный" else "")
        prefix = f"{tag}, " if tag else ""
        out.append(f'  {st["готово"]}/{st["всего"]} готово :{prefix}{node_id(s.get("ключ") or "s")}, {start}, {end}')
    return "\n".join(out)


# --- treeView: иерархия целей ----------------------------------------------

def treeview() -> str:
    """Цель → проект → веха → задачи. Тип treeView из вашего списка."""
    tickets = store.load_tickets()
    roots = [k for k, t in tickets.items() if t.get("тип") == "цель"]
    if not roots:
        roots = [k for k, t in tickets.items() if t.get("тип") == "проект"]

    out = ["treeView-beta"]
    seen: set[str] = set()

    def walk(key: str, level: int):
        if key in seen or level > 4:
            return
        seen.add(key)
        t = tickets.get(key, {})
        st = store.progress_of(key)
        out.append("  " * (level + 1)
                   + f'{label(t.get("название") or key, 40)} ({st["процент"]}%)')
        for child in store.children_of(key):
            walk(child, level + 1)

    for r in sorted(roots):
        walk(r, 0)
    return "\n".join(out) if len(out) > 1 else "treeView-beta\n  Целей пока нет"


# --- sankey: поток задач по статусам ---------------------------------------

def sankey() -> str:
    """Поток «направление → статус». Ваша мечта про красивый sankey.

    Строим по фактическому распределению: сколько задач каждого направления
    в каком статусе. Значение — число задач, поэтому диаграмма честно
    показывает, где скапливается работа.
    """
    tickets = store.load_tickets()
    flows: dict[tuple[str, str], int] = {}
    for key, t in tickets.items():
        direction = key.rsplit("-", 1)[0]
        status = t.get("статус_тег") or "без статуса"
        flows[(direction, status)] = flows.get((direction, status), 0) + 1

    if not flows:
        return "sankey\n\nNo data,Empty,1"

    # ВАЖНО: парсер sankey в mermaid 11.17 НЕ принимает кириллицу — падает
    # даже на одной букве («А,Б,5» → Parse error), и кавычки не спасают.
    # Проверено рендером на пяти вариантах. Статусы поэтому переводим по
    # словарю, а не транслитерируем: «zakryt» читается хуже, чем «Done».
    out = ["sankey", ""]
    for (src, dst), n in sorted(flows.items(), key=lambda x: (-x[1], x[0])):
        # Запятая — разделитель колонок, в подписи её быть не может.
        out.append(f"{translit(src)},{STATUS_EN.get(dst, translit(dst))},{n}")
    return "\n".join(out)


# Латинские подписи статусов для sankey (кириллицу его парсер не принимает).
# Осмысленный перевод вместо транслитерации: «Done» понятнее, чем «zakryt».
STATUS_EN = {
    "в беклоге": "Backlog",
    "бэклог": "Backlog",
    "анализ": "Analysis",
    "планирование": "Planning",
    "разработка": "In progress",
    "тестирование": "Testing",
    "ревью": "Review",
    "закрыт": "Done",
}


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text: str) -> str:
    """Кириллица → латиница для диаграмм, где парсер её не принимает.

    Нужен только sankey — остальные типы кириллицу держат (проверено).
    Пробел заменяем на подчёркивание: в sankey он допустим, но склеивает
    подписи визуально.
    """
    out = []
    for ch in str(text or ""):
        low = ch.lower()
        if low in _TRANSLIT:
            rep = _TRANSLIT[low]
            out.append(rep.upper() if ch.isupper() and rep else rep)
        elif ch.isalnum() or ch in "_-":
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    return "".join(out) or "x"


# --- ishikawa: разбор бага -------------------------------------------------

def ishikawa(key: str) -> str:
    """Заготовка «рыбьей кости» под разбор причин.

    Данных о причинах в трекере нет — их пишет человек. Поэтому здесь
    честная заготовка по 6M с названием задачи в качестве проблемы,
    а не выдуманные причины.
    """
    tickets = store.load_tickets()
    t = tickets.get(key, {})
    problem = label(t.get("название") or key, 40)
    return "\n".join([
        "ishikawa-beta",
        f'  problem "{problem}"',
        '  cause "Процесс"',
        '    cause "шаг не описан"',
        '  cause "Данные"',
        '    cause "источник молча поменялся"',
        '  cause "Инструменты"',
        '    cause "проверка не краснела"',
        '  cause "Люди"',
        '    cause "знание не зафиксировано"',
    ])


# --- pie: распределение ----------------------------------------------------

def pie(tickets: dict[str, dict] | None = None) -> str:
    tickets = tickets if tickets is not None else store.load_tickets()
    counts: dict[str, int] = {}
    for t in tickets.values():
        tag = t.get("статус_тег") or "без статуса"
        counts[tag] = counts.get(tag, 0) + 1
    out = ["pie showData", "  title Задачи по статусам"]
    for tag, n in sorted(counts.items(), key=lambda x: -x[1]):
        out.append(f'  "{label(tag, 20)}" : {n}')
    return "\n".join(out)


GENERATORS = {
    "graph": ("Граф связей", "flowchart"),
    "mindmap": ("Эпик деревом", "mindmap"),
    "kanban": ("Канбан", "kanban"),
    "gantt": ("Спринты", "gantt"),
    "treeview": ("Иерархия целей", "treeView"),
    "sankey": ("Поток по статусам", "sankey"),
    "pie": ("Распределение", "pie"),
    "ishikawa": ("Разбор причин", "ishikawa"),
}


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "graph"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    fn = {"graph": graph, "kanban": kanban, "gantt": gantt, "treeview": treeview,
          "sankey": sankey, "pie": pie}.get(which)
    if fn:
        print(fn())
    elif which == "mindmap":
        print(mindmap(arg or "DEMO-1"))
    elif which == "ishikawa":
        print(ishikawa(arg or "DEMO-1"))
    else:
        print(f"Неизвестный тип: {which}. Доступны: {', '.join(GENERATORS)}",
              file=sys.stderr)
        sys.exit(1)
