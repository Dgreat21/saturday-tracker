#!/usr/bin/env python3
"""Патч записи для собранного фронта трекера.

Бандл собран как read-only витрина: автор оставил шесть мест с пометкой
«мокап», где вместо сохранения показывается тост. Здесь эти места
заменяются реальными вызовами API сервера, который пишет через
`tracker_store` — лок `ctx_lock`, атомарная запись, git-коммит на правку.

Почему заменой строк, а не переопределением методов: код приложения лежит
в бандле ЧИТАЕМЫМ исходником (2223 строки внутри JSON-строки в
`<script type="__bundler/template">`), а не минифицированным. Каждый мокап —
уникальная строка с `this` и `t` в области видимости. Замена такой строки
адресна и проверяема; попытка добраться до внутренностей фреймворка
(`x-dc`, `DCLogic`) означала бы завязку на его недокументированное
устройство.

ВАЖНО: патч применяется НА ВЫДАЧЕ (`tracker_ui.app_html`). Файл на диске не
меняется — он должен продолжать открываться двойным кликом офлайн, где
сервера нет и писать некуда. Пересоберёте бандл — патч применится заново,
а если формат изменится, `verify()` скажет об этом вслух, а не молча
отдаст нерабочую страницу.
"""

from __future__ import annotations

import json

# Каждая пара — (что ищем в исходнике бандла, чем заменяем).
# Ключ идёт первым элементом: по нему `verify()` отчитывается, какие замены
# не сработали. Порядок не важен, но менять формулировки — только вместе
# с прогоном `tracker_selftest.py` (класс 8).
REPLACEMENTS: list[tuple[str, str, str]] = [

    # 1. Статус: перетаскивание на доске и выбор колонки.
    (
        "applyStatus",
        "this.toast(key + ': ' + (prev || '—') + ' → ' + st + ' · git-коммит (мокап)');",
        "window.__trkWrite(this, '/api/update', {'ключ': key, 'статус_тег': st},"
        " key + ': ' + (prev || '—') + ' → ' + st);",
    ),

    # 2. Статус из карточки (выпадающий список «Свойства»).
    (
        "setPStatus",
        "setPStatus: () => this.toast('Мокап: статус поменялся бы с git-коммитом'),",
        "setPStatus: (e) => window.__trkWrite(this, '/api/update',"
        " {'ключ': t.key, 'статус_тег': e.target.value},"
        " t.key + ': статус → ' + e.target.value),",
    ),

    # 3. Спринт.
    (
        "setPSprint",
        "setPSprint: () => this.toast('Мокап: задача попала бы в спринт'),",
        "setPSprint: (e) => window.__trkWrite(this, '/api/update',"
        " {'ключ': t.key, 'спринт': e.target.value},"
        " t.key + ': спринт → ' + (e.target.value || 'без спринта')),",
    ),

    # 4. Связь. Поля формы в бандле не связаны со state, поэтому значения
    #    читаются из DOM по клику — переписывать разметку не требуется.
    (
        "fakeLink",
        "fakeLink: () => this.toast('Мокап: ребро добавилось бы в links.csv одной строкой'),",
        "fakeLink: (e) => window.__trkLink(this, t.key, e),",
    ),

    # 5. Комментарий на странице тикета.
    (
        "addComment",
        "addComment: () => this.toast('Мокап: реплика ушла бы в comments/' + t.key + '.md'),",
        "addComment: () => window.__trkComment(this, t.key, this.state.cmDraft,"
        " this.state.commentAuthor, 'cmDraft'),",
    ),

    # 6. Комментарий из выдвижной панели (там своё поле ввода — dDraft).
    (
        "dAddComment",
        "base.dAddComment = () => { this.toast('Мокап: реплика ' + s.commentAuthor"
        " + ' ушла бы в comments/' + t.key + '.md'); this.setState({ dDraft: '' }); };",
        "base.dAddComment = () => window.__trkComment(this, t.key, this.state.dDraft,"
        " this.state.commentAuthor, 'dDraft');",
    ),

    # 7. Создание задачи.
    (
        "createTask",
        "createTask: () => { this.toast(cd + '-' + (maxId + 1) + ' — создана бы:"
        " лок, append в CSV, git-коммит (мокап)'); }",
        "createTask: (e) => window.__trkCreate(this, cd, e)",
    ),

    # Четвёртое меню фильтров — «показ». Логика соседей и одиночек в графе
    # уже была, но включить её было нечем: `gNeighbors` никто не выставлял,
    # и код лежал мёртвым — ровно тот случай, когда функция «есть», но её
    # нет. Счётчик у пункта показывает, сколько узлов он ДОБАВИТ к текущему
    # срезу, а не сколько их всего: иначе цифра ни о чём не говорит.
    # Роутер должен знать текущий тикет ФАКТОМ, а не догадкой по разметке.
    # Первая версия искала `[data-trk-key]` (такого атрибута в бандле нет)
    # и разбирала <h1> — а там заголовок, не ключ. Итог: ключ не находился
    # никогда, адрес всегда переписывался на «/», и переоткрытие тикета
    # в уже открытом окне не работало.
    (
        "openPage",
        "openPage(key) { this.setState({ page: 'ticket', pageKey: key, drawerKey: null, cmdk: false, commentTab: 'write' });",
        "openPage(key) { window.__trkCurrent = key; this.setState({ page: 'ticket', pageKey: key, drawerKey: null, cmdk: false, commentTab: 'write' });",
    ),
    # trkGo — второй путь открытия карточки (клик по узлу графа,
    # переход из списка). Он тоже обязан публиковать ключ, иначе адрес
    # отстанет от того, что на экране.
    (
        "trkGo",
        "if (!host || !node) { this.setState({ page: 'ticket', pageKey: key, drawerKey: null, cmdk: false }); this.needComments(key); return; }",
        "if (!host || !node) { window.__trkCurrent = key; this.setState({ page: 'ticket', pageKey: key, drawerKey: null, cmdk: false }); this.needComments(key); return; }",
    ),
    (
        "diagMenus",
        "        this.menuFor('gLinkTypes', 'все связи', 'связь', '150px', ['блокирует', 'входит в', 'реализует', 'относится к'],\n          (x) => d.links.filter(l => l.type === x).length)\n      ],",
        "        this.menuFor('gLinkTypes', 'все связи', 'связь', '150px', ['блокирует', 'входит в', 'реализует', 'относится к'],\n          (x) => d.links.filter(l => l.type === x).length),\n        this.menuFor('gShow', 'связанные', 'показ', '180px',\n          ['соседи из других очередей', 'задачи без связей'],\n          (x) => {\n            const bk = this.byKey();\n            if (x === 'задачи без связей') {\n              const inL = new Set();\n              d.links.forEach(l => { inL.add(l.from); inL.add(l.to); });\n              return this.gTasks().filter(t => !inL.has(t.key)).length;\n            }\n            const sc = new Set(this.gTasks().map(t => t.key));\n            const out = new Set();\n            d.links.forEach(l => {\n              if (sc.has(l.from) && !sc.has(l.to) && bk[l.to]) out.add(l.to);\n              if (sc.has(l.to) && !sc.has(l.from) && bk[l.from]) out.add(l.from);\n            });\n            return out.size;\n          })\n      ],",
    ),
    # Кнопка «сбросить» обязана чистить и новое меню, иначе фильтр
    # останется включённым, а признаков этого на экране не будет.
    (
        "diagReset",
        'diagReset: () => this.setState({ gDirs: [], gStatuses: [], gLinkTypes: [], openMenu: null })',
        'diagReset: () => this.setState({ gDirs: [], gStatuses: [], gLinkTypes: [], gShow: [], openMenu: null })',
    ),
    (
        "diagResetDisp",
        "diagResetDisp: ((s.gDirs || []).length + (s.gStatuses || []).length + (s.gLinkTypes || []).length) ? 'inline-flex' : 'none',",
        "diagResetDisp: ((s.gDirs || []).length + (s.gStatuses || []).length + (s.gLinkTypes || []).length + (s.gShow || []).length) ? 'inline-flex' : 'none',",
    ),
    (
        "graphSrc",
        "const keys = new Set(); links.forEach(l => { keys.add(l.from); keys.add(l.to); });",
        "const keys = new Set(); links.forEach(l => { keys.add(l.from); keys.add(l.to); });"
        " var _show = this.state.gShow || [];"
        " if (_show.indexOf('соседи из других очередей') < 0) { links.forEach(l => {"
        "   if (bk[l.from] && !this.gPass(bk[l.from])) keys.delete(l.from);"
        "   if (bk[l.to] && !this.gPass(bk[l.to])) keys.delete(l.to); }); }"
        " if (_show.indexOf('задачи без связей') >= 0) { this.gTasks().forEach(t => keys.add(t.key)); }",
    ),
]


# Рантайм записи. Вставляется в <head> страницы приложения.
#
# Общая политика на все действия: оптимистично ничего не показываем как
# сохранённое, пока сервер не подтвердил, а после подтверждения ПЕРЕЧИТЫВАЕМ
# файлы. Иначе интерфейс начал бы жить своей моделью данных, и первое же
# расхождение с CSV осталось бы незамеченным.
RUNTIME = r"""
<script>
(function () {
  'use strict';
  var API = window.__trkApi || '';

  function post(path, payload) {
    return fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: 'сервер вернул не JSON (HTTP ' + r.status + ')' };
      });
    });
  }

  // Перечитать CSV: файл — истина. Заодно сбрасываем statusOv — накопленные
  // в памяти правки статуса, которые иначе перекрыли бы реальные данные.
  function reload(app) {
    var f = [['tasks', 'tasks.csv'], ['goals', 'goals.csv'],
             ['links', 'links.csv'], ['sprints', 'sprints.csv']];
    return Promise.all(f.map(function (p) {
      return fetch(API + '/data/' + p[1] + '?t=' + Date.now())
        .then(function (r) { return r.text(); });
    })).then(function (texts) {
      var d = {};
      f.forEach(function (p, i) {
        d[p[0]] = app.csv(texts[i]).map(function (r) { return app.mapItem(r); });
      });
      app.setState({ data: d, statusOv: {} });
    });
  }

  function fail(app, res) {
    app.toast('Не сохранилось: ' + ((res && res.error) || 'сервер не ответил'));
    return reload(app);   // показываем то, что реально в файлах
  }

  function ok(app, msg, res) {
    app.toast(msg + (res && res.commit ? ' · коммит ' + res.commit : ''));
    return reload(app);
  }

  window.__trkWrite = function (app, path, payload, msg) {
    return post(path, payload).then(function (res) {
      if (!res.ok) return fail(app, res);
      if (res['изменено'] && res['изменено'].length === 0) {
        app.toast('Изменений нет');
        return reload(app);
      }
      return ok(app, msg, res);
    }).catch(function (e) { return fail(app, { error: String(e.message || e) }); });
  };

  window.__trkComment = function (app, key, text, author, field) {
    text = (text || '').trim();
    if (!text) { app.toast('Пустой комментарий'); return; }
    return post('/api/comment', { 'ключ': key, 'текст': text,
                                  'автор': author || 'user' })
      .then(function (res) {
        if (!res.ok) return fail(app, res);
        var patch = {}; patch[field] = '';
        app.setState(patch);
        // Комментарии кэшируются в state — выбрасываем ключ, чтобы
        // подтянулись заново уже с сервера.
        app.setState(function (s) {
          var c = {}; for (var k in s.comments) if (k !== key) c[k] = s.comments[k];
          return { comments: c };
        });
        return ok(app, 'Комментарий добавлен', res).then(function () {
          app.needComments(key);
        });
      }).catch(function (e) { return fail(app, { error: String(e.message || e) }); });
  };

  window.__trkLink = function (app, fromKey, ev) {
    var box = ev && ev.target ? ev.target.parentElement : null;
    var sel = box ? box.querySelector('select') : null;
    var inp = box ? box.querySelector('input') : null;
    if (!sel || !inp) {   // разметка формы изменилась — честно об этом говорим
      app.toast('Не нашёл поля формы связи');
      return;
    }
    var to = (inp.value || '').trim();
    if (!to) { app.toast('Укажите ключ тикета'); return; }
    return post('/api/link/add', { 'откуда': fromKey, 'тип': sel.value, 'куда': to })
      .then(function (res) {
        if (!res.ok) return fail(app, res);
        inp.value = '';
        app.setState({ linkForm: false });
        return ok(app, res['добавлено'] ? 'Связь добавлена' : 'Такая связь уже есть', res);
      }).catch(function (e) { return fail(app, { error: String(e.message || e) }); });
  };

  window.__trkCreate = function (app, dir, ev) {
    // Поля формы создания не связаны со state (в бандле у них noop), поэтому
    // читаем значения из DOM. Ищем от кнопки вверх — так не зависим от того,
    // какой контейнер обёрнут вокруг формы.
    var root = ev && ev.target ? ev.target.closest('section, article, div.card, div') : null;
    var scope = root || document;
    var title = scope.querySelector('input.input:not([type=radio]):not([type=checkbox])');
    var areas = scope.querySelectorAll('textarea.input');
    var ty = scope.querySelector('input[name=ctype]:checked');
    var au = scope.querySelector('input[name=cauthor]:checked');
    var name = title ? (title.value || '').trim() : '';
    if (!name) { app.toast('Название обязательно'); return; }
    var payload = {
      'направление': dir, 'название': name,
      'описание': areas[0] ? areas[0].value : '',
      'dod': areas[1] ? areas[1].value : '',
      'тип': (ty && ty.value) || 'task',
      'автор': (au && au.value) || 'user'
    };
    return post('/api/create', payload).then(function (res) {
      if (!res.ok) return fail(app, res);
      if (title) title.value = '';
      Array.prototype.forEach.call(areas, function (a) { a.value = ''; });
      return ok(app, 'Создан ' + res['ключ'], res).then(function () {
        if (window.trkGo) window.trkGo(res['ключ']);
      });
    }).catch(function (e) { return fail(app, { error: String(e.message || e) }); });
  };
})();
</script>
"""


# Роутер прямых ссылок: /t/DEMO-2 открывает карточку сразу.
#
# Бандл — чистое SPA: ни location, ни history он не трогает, вся навигация
# живёт в состоянии. Поэтому ссылкой на конкретный тикет поделиться было
# нечем — приходилось объяснять словами «открой трекер и найди DEMO-2».
#
# Делаем две вещи: открываем карточку по ключу, который передал сервер, и
# отражаем текущий тикет в адресной строке (replaceState, без записи в
# историю — иначе «назад» превращалось бы в перемотку по каждому клику).
ROUTER = r"""
<script>
(function () {
  'use strict';
  var API = window.__trkApi || '';

  function go(key, tries) {
    tries = tries || 0;
    if (window.trkGo) { window.trkGo(key); return; }
    // Приложение монтируется асинхронно: ждём появления навигатора,
    // но не бесконечно — иначе молча висели бы при поломке бандла.
    if (tries < 100) setTimeout(function () { go(key, tries + 1); }, 100);
  }

  if (window.__trkOpen) go(window.__trkOpen, 0);

  // Держим адресную строку в согласии с открытой карточкой, чтобы ссылку
  // можно было скопировать прямо из браузера.
  var last = null;
  setInterval(function () {
    try {
      // Ключ берём из того, что публикует само приложение (openPage),
      // а не вытаскиваем из разметки: разметка меняется при каждой
      // пересборке бандла, а состояние — нет.
      var key = window.__trkCurrent || null;
      var want = key ? '/t/' + key : '/';
      if (want !== last) {
        last = want;
        history.replaceState(null, '', want);
      }
    } catch (e) { /* адресная строка — украшение, ронять из-за неё нельзя */ }
  }, 700);
})();
</script>
"""


def apply(template: str, api_origin: str,
          deep_key: str | None = None) -> tuple[str, list[str]]:
    """Заменить мокапы на вызовы API. Возвращает (код, список несработавших)."""
    missed: list[str] = []
    out = template
    for name, old, new in REPLACEMENTS:
        if old not in out:
            missed.append(name)
            continue
        out = out.replace(old, new, 1)

    runtime = RUNTIME.replace("__TRK_API__", api_origin)
    boot = f'<script>window.__trkApi = "{api_origin}";'
    if deep_key:
        # Ключ уезжает в страницу отдельной переменной, а не в URL-парсере на
        # клиенте: сервер уже знает, что просили, и незачем разбирать путь дважды.
        safe = json.dumps(deep_key, ensure_ascii=False)
        boot += f"window.__trkOpen = {safe};"
    boot += "</script>"
    inject = boot + runtime + ROUTER
    if "</head>" in out:
        out = out.replace("</head>", inject + "</head>", 1)
    else:
        missed.append("<head> не найден — рантайм не вставлен")
    return out, missed


# Схлопывания вкладок здесь больше нет — и это осознанное решение.
#
# Задача «ссылки открываются отдельными окнами» решилась системной
# настройкой macOS `AppleWindowTabbingMode = always`: ссылки извне идут
# вкладками в одно окно. Проверено фактом: 28 → 30 вкладок, окно осталось одно.
#
# Собственный механизм (localStorage-владелец + BroadcastChannel + заглушка)
# решал уже другую задачу — не плодить дубли интерфейса, — но начал вредить:
# протухший владелец делал заглушкой даже ПЕРВУЮ вкладку, и человек видел
# «открыт в другой вкладке», когда никакой другой не было. Инструмент, который
# врёт о состоянии, хуже его отсутствия.
#
# Если задача вернётся, начинать надо не с localStorage: у вкладок нет
# надёжного способа узнать, что владелец умер, — только TTL, а он даёт
# либо залипание, либо ложные перехваты.
TAB_DEDUP = ""
