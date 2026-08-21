#!/usr/bin/env python3
"""Ядро трекера: чтение и запись файлов как источника истины.

Отдельный модуль от UI намеренно: запись — самая опасная часть, и её нужно
уметь тестировать без веб-сервера. UI импортирует это и ничего не пишет сам.

Гарантии записи (в порядке, в котором они срабатывают):

1. **Лок.** Пишем только под `ctx_lock.py` — тем же мьютексом, которым
   пользуются агенты. Иначе UI и агент, работающие одновременно, затрут
   друг друга.
2. **Атомарность.** Пишем во временный файл рядом и делаем `os.replace`.
   На POSIX это атомарная подмена: читатель видит либо старый файл целиком,
   либо новый, но никогда — половину. Обрыв на середине записи не оставляет
   покорёженный CSV.
3. **fsync.** Перед подменой сбрасываем буферы на диск: без этого «файл
   записан» означает лишь «данные в page cache», и внезапная перезагрузка
   съедает правку, о которой UI отчитался успехом.
4. **`обновлено`.** Проставляется автоматически при любой правке поля —
   правило §3 README трекера, которое руками соблюдать забывают.
5. **git-коммит.** Каждая правка — отдельный коммит в git-репозитории данных
   (если он есть). Это и история «кто/когда/что», и откат любой правки.

Ошибка на любом шаге поднимается наверх: UI обязан показать её человеку,
а не проглотить. «Сохранилось, но нет» — худший из возможных исходов.
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# --- Где лежат данные -------------------------------------------------------
#
# Приложение отделено от данных: код версионируется здесь, тикеты живут где
# угодно. Каталог данных берётся из (в порядке приоритета):
#
#   1) переменная окружения SATURDAY_TRACKER_DATA
#   2) ./tracker рядом с текущим рабочим каталогом
#   3) демо-данные из поставки — чтобы `python -m saturday_tracker`
#      сразу что-то показывал, а не падал на пустоте
#
# Демо лежит ВНУТРИ пакета (`sample_data/`), а не в `examples/` репозитория:
# иначе после `pip install` каталога рядом не окажется и трекер стартует пустым.
# `examples/tracker` в репозитории — копия для тех, кто просто склонировал.
#
# Менять пути на лету умеет set_data_dir() — им пользуются тесты и UI.

_PKG = Path(__file__).resolve().parent
_DEFAULT_SAMPLE = _PKG / "sample_data"

LOCK_TOOL = _PKG / "ctx_lock.py"
ENCODING = "utf-8-sig"

# Автор по умолчанию для правок из UI/CLI. Переопределяется
# SATURDAY_TRACKER_AUTHOR или аргументом author= в каждой функции.
DEFAULT_AUTHOR = os.environ.get("SATURDAY_TRACKER_AUTHOR") or "user"


def _resolve_data_dir() -> Path:
    env = os.environ.get("SATURDAY_TRACKER_DATA")
    if env:
        return Path(env).expanduser().resolve()
    local = Path.cwd() / "tracker"
    if local.is_dir():
        return local.resolve()
    return _DEFAULT_SAMPLE.resolve()


def set_data_dir(path) -> Path:
    """Переключить каталог данных (тесты, UI с флагом --data)."""
    global TRACKER, COMMENTS, TASKS, GOALS, LINKS, SPRINTS
    TRACKER = Path(path).expanduser().resolve()
    COMMENTS = TRACKER / "comments"
    TASKS = TRACKER / "tasks.csv"
    GOALS = TRACKER / "goals.csv"
    LINKS = TRACKER / "links.csv"
    SPRINTS = TRACKER / "sprints.csv"
    return TRACKER


set_data_dir(_resolve_data_dir())

STATUS_TAGS = ["в беклоге", "анализ", "планирование", "разработка",
               "тестирование", "ревью", "закрыт"]
LINK_TYPES = ["блокирует", "входит в", "реализует", "относится к", "дублирует"]

# Статусы, при которых задача считается сделанной. Вынесено в константу:
# и доска, и burndown, и прогресс целей должны трактовать «готово» одинаково —
# разъехавшиеся трактовки дали бы три разных ответа на один вопрос.
DONE_TAGS = {"закрыт"}

SPRINT_STATUSES = ["план", "активный", "завершён"]

# Поля, которые UI имеет право менять. Всё остальное (ключ, создано) —
# либо идентичность записи, либо служебное: правка через UI их не касается.
EDITABLE = ["тип", "название", "автор", "согласовано", "статус_тег", "статус",
            "описание", "dod", "резолюция_тег", "резолюция", "спринт"]


class TrackerError(Exception):
    """Пользовательская ошибка: показывается человеку, не трейсбеком."""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --- лок -------------------------------------------------------------------

@contextmanager
def locked(*targets: str, owner: str = "tracker-ui", wait: int = 15):
    """Захватить локи на файлы трекера на время записи.

    Берём в отсортированном порядке: два процесса, берущие один и тот же
    набор файлов, не встанут в клинч, если оба идут по одному порядку.
    """
    taken: list[str] = []
    try:
        for t in sorted(targets):
            res = subprocess.run(
                ["python3", str(LOCK_TOOL), "acquire", t, "--wait", str(wait),
                 "--owner", owner],
                capture_output=True, text=True, cwd=str(TRACKER))
            if res.returncode != 0:
                raise TrackerError(
                    f"Файл {t} занят другим процессом (лок не отдали за {wait} с). "
                    f"Повторите позже или снимите протухшие локи: "
                    f"python3 -m saturday_tracker.ctx_lock clean")
            taken.append(t)
        yield
    finally:
        for t in reversed(taken):
            subprocess.run(["python3", str(LOCK_TOOL), "release", t],
                           capture_output=True, cwd=str(TRACKER))


# --- атомарная запись ------------------------------------------------------

def atomic_write(path: Path, text: str) -> None:
    """Записать файл целиком, атомарно и с fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding=ENCODING, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # атомарная подмена
        # Каталог тоже синкаем: без этого переименование может не пережить
        # внезапную перезагрузку, хотя содержимое файла уже на диске.
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def detect_newline(path: Path, default: str = "\r\n") -> str:
    """Каким переводом строки написан файл.

    Критично: файлы трекера писались модулем csv с дефолтным `\\r\\n`. Запиши
    мы `\\n` — git показал бы ИЗМЕНЁННЫМИ ВСЕ строки при правке одного поля,
    то есть ровно ту помойку в диффе, ради устранения которой затевался
    перенос JSON из ячеек. Ловили на этом: 38 строк в дифф на одну правку.
    """
    if not path.exists():
        return default
    with path.open("rb") as fh:
        head = fh.read(65536)
    if b"\r\n" in head:
        return "\r\n"
    if b"\n" in head:
        return "\n"
    return default


def _write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    from io import StringIO
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator=detect_newline(path),
                       quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: (r.get(c) or "") for c in cols})
    atomic_write(path, buf.getvalue())


# --- git -------------------------------------------------------------------

def git_root() -> Path | None:
    """Корень git-репозитория, в котором лежат данные, либо None.

    Каталог данных **не обязан** быть под git: тогда трекер просто работает
    без истории правок. Молча — это осознанно: git здесь приятная добавка,
    а не требование к запуску.
    """
    res = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         cwd=str(TRACKER), capture_output=True, text=True)
    if res.returncode != 0:
        return None
    return Path(res.stdout.strip())


def git_commit(message: str, paths: list[Path]) -> str | None:
    """Закоммитить правку в git-репозиторий данных, если он есть.

    Возвращает короткий хеш; None — если коммитить нечего или данные вне git.
    Ошибка git не откатывает запись в файл: файл — истина, git — история.
    Поэтому проблему коммита показываем, но правку не теряем.
    """
    root = git_root()
    if root is None:
        return None
    rels = [str(p.resolve().relative_to(root)) for p in paths if p.exists()]
    if not rels:
        return None
    subprocess.run(["git", "add", "--", *rels], cwd=str(root),
                   capture_output=True, text=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *rels],
                            cwd=str(root), capture_output=True)
    if staged.returncode == 0:
        return None                     # изменений нет — коммит не нужен
    res = subprocess.run(["git", "commit", "-q", "-m", message, "--", *rels],
                         cwd=str(root), capture_output=True, text=True)
    if res.returncode != 0:
        raise TrackerError(
            "Правка записана в файл, но git-коммит не прошёл: "
            + (res.stderr.strip() or res.stdout.strip()))
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=str(root), capture_output=True, text=True)
    return out.stdout.strip()


# --- чтение ----------------------------------------------------------------

def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding=ENCODING, newline="") as fh:
        return [r for r in csv.DictReader(fh) if any((v or "").strip() for v in r.values())]


def csv_columns(path: Path) -> list[str]:
    with path.open(encoding=ENCODING, newline="") as fh:
        return next(csv.reader(fh))


def load_tickets() -> dict[str, dict]:
    """Все тикеты обоих файлов, с пометкой источника."""
    out: dict[str, dict] = {}
    for path, kind in ((TASKS, "tasks"), (GOALS, "goals")):
        for r in read_csv_rows(path):
            key = (r.get("ключ") or "").strip()
            if key:
                r["_файл"] = kind
                out[key] = r
    return out


def load_links() -> list[dict]:
    return read_csv_rows(LINKS)


def load_sprints() -> list[dict]:
    """Спринты из sprints.csv. Файла нет — спринтов нет, это не ошибка."""
    return read_csv_rows(SPRINTS)


def sprint_stats(key: str) -> dict:
    """Состав и прогресс спринта.

    «Готово» считается по DONE_TAGS — той же константе, что использует доска
    и прогресс целей. Три разных трактовки «сделано» на трёх экранах — верный
    способ получить три разных ответа на один вопрос.
    """
    tasks = [t for t in load_tickets().values() if (t.get("спринт") or "") == key]
    done = [t for t in tasks if t.get("статус_тег") in DONE_TAGS]
    return {"всего": len(tasks), "готово": len(done),
            "процент": round(100 * len(done) / len(tasks)) if tasks else 0,
            "задачи": tasks}


def children_of(key: str) -> list[str]:
    """Кто входит в этот тикет (иерархия цель → проект → веха → задача).

    Читается из links.csv, а не из отдельного поля: заведи мы «родителя»
    колонкой — получили бы второй источник истины про одно и то же ребро.
    """
    return sorted(e["откуда"] for e in load_links()
                  if e.get("тип") in ("входит в", "реализует") and e.get("куда") == key)


def progress_of(key: str, _seen: set | None = None) -> dict:
    """Прогресс тикета по потомкам, рекурсивно.

    `_seen` защищает от цикла в связях: `A входит в B входит в A` иначе
    ушло бы в бесконечную рекурсию. Цикл — данные кривые, но падать всё
    приложение из-за этого не должно.
    """
    seen = _seen or set()
    if key in seen:
        return {"всего": 0, "готово": 0, "процент": 0}
    seen = seen | {key}

    tickets = load_tickets()
    kids = children_of(key)
    if not kids:
        done = 1 if tickets.get(key, {}).get("статус_тег") in DONE_TAGS else 0
        return {"всего": 1, "готово": done, "процент": done * 100}

    total = done = 0
    for k in kids:
        st = progress_of(k, seen)
        total += st["всего"]
        done += st["готово"]
    return {"всего": total, "готово": done,
            "процент": round(100 * done / total) if total else 0}


def links_for(key: str) -> list[dict]:
    """Связи тикета с обеих сторон.

    Обратная сторона ВЫВОДИТСЯ, а не читается из файла: в links.csv ребро
    лежит один раз. Ровно та причина, по которой формат и менялся.
    """
    inverse = {"блокирует": "блокируется", "относится к": "относится к",
               "дублирует": "дублирует"}
    out = []
    for e in load_links():
        src, rel, dst = e.get("откуда"), e.get("тип"), e.get("куда")
        if src == key:
            out.append({"тип": rel, "ключ": dst, "сторона": "прямая"})
        elif dst == key:
            out.append({"тип": inverse.get(rel, f"{rel} (обратная)"),
                        "ключ": src, "сторона": "обратная"})
    return sorted(out, key=lambda x: (x["тип"], x["ключ"]))


_HEAD = re.compile(r"^##\s+(?P<author>.+?)(?:\s+·\s+(?P<when>[\d\-: ]+))?\s*$")


def load_comments(key: str) -> list[dict]:
    path = COMMENTS / f"{key}.md"
    if not path.exists():
        return []
    items, author, when, buf = [], None, None, []

    def flush():
        nonlocal buf
        text = "\n".join(buf).strip()
        if author is not None and text:
            items.append({"автор": author, "время": when or "", "текст": text})
        buf = []

    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEAD.match(line)
        if m:
            flush()
            author = m.group("author").strip()
            when = (m.group("when") or "").strip()
        elif line.startswith("# "):
            continue
        else:
            buf.append(line)
    flush()
    return items


# --- запись ----------------------------------------------------------------

def update_ticket(key: str, changes: dict[str, str], *, author: str = DEFAULT_AUTHOR) -> dict:
    """Изменить поля тикета. Возвращает {'изменено': [...], 'commit': hash}.

    `обновлено` проставляется автоматически — правило §3 README трекера.
    Если ни одно поле фактически не поменялось, файл не трогаем и коммит
    не делаем: пустые коммиты замусоривают историю.
    """
    tickets = load_tickets()
    if key not in tickets:
        raise TrackerError(f"Тикет {key} не найден")

    kind = tickets[key]["_файл"]
    path = TASKS if kind == "tasks" else GOALS
    target = f"mastery/tracker/{'tasks' if kind == 'tasks' else 'goals'}.csv"

    bad = [f for f in changes if f not in EDITABLE]
    if bad:
        raise TrackerError(f"Эти поля не редактируются через UI: {', '.join(bad)}")
    if "статус_тег" in changes and changes["статус_тег"] not in STATUS_TAGS:
        raise TrackerError(f"Недопустимый статус_тег: {changes['статус_тег']}")

    with locked(target):
        rows = read_csv_rows(path)
        cols = csv_columns(path)
        changed: list[str] = []
        for r in rows:
            if (r.get("ключ") or "").strip() != key:
                continue
            for field, value in changes.items():
                if (r.get(field) or "") != (value or ""):
                    r[field] = value
                    changed.append(field)
            if changed:
                r["обновлено"] = now()
            break
        if not changed:
            return {"изменено": [], "commit": None}
        _write_csv(path, cols, rows)

    # Коммит вне лока: git может быть медленным, а держать мьютекс дольше,
    # чем идёт запись файла, незачем.
    msg = f"{key}: {', '.join(changed)} ({author})"
    return {"изменено": changed, "commit": git_commit(msg, [path])}


def add_comment(key: str, text: str, *, author: str = DEFAULT_AUTHOR) -> dict:
    """Добавить реплику в comments/<КЛЮЧ>.md.

    Дописываем в конец существующего файла, а не перезаписываем целиком:
    так параллельная правка руками теряет максимум одну реплику, а не всё
    обсуждение. Тикет при этом помечается обновлённым.
    """
    text = (text or "").strip()
    if not text:
        raise TrackerError("Пустой комментарий")
    if key not in load_tickets():
        raise TrackerError(f"Тикет {key} не найден")

    path = COMMENTS / f"{key}.md"
    target = f"mastery/tracker/comments/{key}.md"
    stamp = now()

    with locked(target):
        if path.exists():
            body = path.read_text(encoding="utf-8").rstrip()
            new = f"{body}\n\n## {author} · {stamp}\n\n{text}\n"
        else:
            new = f"# Комментарии: {key}\n\n## {author} · {stamp}\n\n{text}\n"
        atomic_write(path, new)

    # Комментарий — тоже изменение тикета: двигаем `обновлено`.
    tickets = load_tickets()
    kind = tickets[key]["_файл"]
    tpath = TASKS if kind == "tasks" else GOALS
    ttarget = f"mastery/tracker/{'tasks' if kind == 'tasks' else 'goals'}.csv"
    with locked(ttarget):
        rows = read_csv_rows(tpath)
        cols = csv_columns(tpath)
        for r in rows:
            if (r.get("ключ") or "").strip() == key:
                r["обновлено"] = stamp
                break
        _write_csv(tpath, cols, rows)

    return {"commit": git_commit(f"{key}: комментарий ({author})", [path, tpath])}


def add_link(src: str, rel: str, dst: str, *, author: str = DEFAULT_AUTHOR) -> dict:
    """Добавить связь. Ребро пишется ОДИН раз, обратное не дублируем."""
    tickets = load_tickets()
    for k in (src, dst):
        if k not in tickets:
            raise TrackerError(f"Тикет {k} не найден")
    if src == dst:
        raise TrackerError("Связь тикета с самим собой бессмысленна")
    if rel not in LINK_TYPES:
        raise TrackerError(f"Недопустимый тип связи: {rel}")

    # Симметричные типы нормализуем по алфавиту — иначе (A,B) и (B,A)
    # создадут два ребра, означающих одно и то же.
    if rel in ("относится к", "дублирует") and src > dst:
        src, dst = dst, src

    with locked("mastery/tracker/links.csv"):
        rows = read_csv_rows(LINKS)
        if any(r.get("откуда") == src and r.get("тип") == rel and r.get("куда") == dst
               for r in rows):
            return {"добавлено": False, "commit": None}
        rows.append({"откуда": src, "тип": rel, "куда": dst})
        rows.sort(key=lambda r: (r.get("откуда") or "", r.get("тип") or "", r.get("куда") or ""))
        _write_csv(LINKS, ["откуда", "тип", "куда"], rows)

    return {"добавлено": True,
            "commit": git_commit(f"связь: {src} {rel} {dst} ({author})", [LINKS])}


def remove_link(src: str, rel: str, dst: str, *, author: str = DEFAULT_AUTHOR) -> dict:
    with locked("mastery/tracker/links.csv"):
        rows = read_csv_rows(LINKS)
        keep = [r for r in rows
                if not (r.get("откуда") == src and r.get("тип") == rel and r.get("куда") == dst)]
        if len(keep) == len(rows):
            return {"удалено": False, "commit": None}
        _write_csv(LINKS, ["откуда", "тип", "куда"], keep)
    return {"удалено": True,
            "commit": git_commit(f"связь удалена: {src} {rel} {dst} ({author})", [LINKS])}


def next_key(direction: str) -> str:
    """Следующий id направления: max существующего + 1, без переиспользования."""
    mx = 0
    for key in load_tickets():
        m = re.match(rf"^{re.escape(direction)}-(\d+)$", key)
        if m:
            mx = max(mx, int(m.group(1)))
    return f"{direction}-{mx + 1}"


def create_ticket(direction: str, fields: dict[str, str], *,
                  goals_file: bool = False, author: str = DEFAULT_AUTHOR) -> dict:
    direction = (direction or "").strip().upper()
    if not re.match(r"^[A-ZА-Я_]+$", direction):
        raise TrackerError("Направление — заглавные латинские буквы, напр. DEMO")
    if not (fields.get("название") or "").strip():
        raise TrackerError("Название обязательно")

    path = GOALS if goals_file else TASKS
    target = f"mastery/tracker/{'goals' if goals_file else 'tasks'}.csv"

    with locked(target):
        rows = read_csv_rows(path)
        cols = csv_columns(path)
        key = next_key(direction)
        row = {c: "" for c in cols}
        row.update({c: fields.get(c, "") for c in EDITABLE if c in cols})
        row["ключ"] = key
        row["автор"] = fields.get("автор") or author
        row["статус_тег"] = fields.get("статус_тег") or "в беклоге"
        row["согласовано"] = fields.get("согласовано") or "нет"
        row["создано"] = row["обновлено"] = now()
        rows.append(row)
        _write_csv(path, cols, rows)

    return {"ключ": key, "commit": git_commit(f"{key}: заведён ({author})", [path])}
