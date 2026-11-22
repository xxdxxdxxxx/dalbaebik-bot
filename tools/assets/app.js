/* Clan Analytics — общий скрипт панели: подтверждения, уведомления, панель сохранения, меню. */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ── уведомления ─────────────────────────────────────────────────── */
  window.toast = function (message, kind) {
    var box = $('#toasts');
    if (!box) return;
    var el = document.createElement('div');
    el.className = 'toast' + (kind === 'err' ? ' err' : '');
    el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, kind === 'err' ? 6000 : 3000);
  };

  /* ── подтверждение вместо confirm() ──────────────────────────────── */
  var dlg = null;
  window.confirmDialog = function (message) {
    return new Promise(function (resolve) {
      if (!window.HTMLDialogElement) { resolve(window.confirm(message)); return; }
      if (!dlg) {
        dlg = document.createElement('dialog');
        dlg.className = 'confirm';
        dlg.innerHTML = '<p></p><div class="row"><button type="button" data-no>Отмена</button>' +
          '<button type="button" class="go" data-yes>Подтвердить</button></div>';
        document.body.appendChild(dlg);
      }
      $('p', dlg).textContent = message;
      var done = function (value) { dlg.onclose = null; if (dlg.open) dlg.close(); resolve(value); };
      $('[data-no]', dlg).onclick = function () { done(false); };
      $('[data-yes]', dlg).onclick = function () { done(true); };
      dlg.onclose = function () { resolve(false); };
      dlg.showModal();
      $('[data-no]', dlg).focus();
    });
  };

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-confirm]');
    if (!b || b.tagName === 'FORM') return;
    if (b.dataset.ok === '1') { delete b.dataset.ok; return; }
    e.preventDefault();
    window.confirmDialog(b.getAttribute('data-confirm')).then(function (ok) {
      if (ok) { b.dataset.ok = '1'; b.click(); }
    });
  });
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f.tagName !== 'FORM' || !f.hasAttribute('data-confirm')) return;
    if (f.dataset.ok === '1') { delete f.dataset.ok; return; }
    e.preventDefault();
    var sub = e.submitter;
    window.confirmDialog(f.getAttribute('data-confirm')).then(function (ok) {
      if (!ok) return;
      f.dataset.ok = '1';
      if (f.requestSubmit) f.requestSubmit(sub); else f.submit();
    });
  });

  /* ── меню «⋯»: закрывается по клику снаружи и Esc ───────────────── */
  document.addEventListener('click', function (e) {
    $$('details.menu[open]').forEach(function (m) { if (!m.contains(e.target)) m.open = false; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') $$('details.menu[open]').forEach(function (m) { m.open = false; });
  });

  /* ── сортируемые заголовки: клавиатура и aria-sort ───────────────── */
  $$('th.sortable').forEach(function (th) {
    th.tabIndex = 0;
    th.setAttribute('role', 'columnheader');
    th.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); th.click(); }
    });
  });
  document.addEventListener('click', function (e) {
    var th = e.target.closest('th.sortable');
    if (!th) return;
    var table = th.closest('table');
    $$('th.sortable', table).forEach(function (x) {
      var d = x.dataset.dir;
      if (d) x.setAttribute('aria-sort', d === 'asc' ? 'ascending' : 'descending');
      else x.removeAttribute('aria-sort');
    });
  });

  /* ── баннеры: убрать служебные параметры из адреса и скрыть сами ── */
  try {
    var url = new URL(location.href), cut = false;
    ['saved', 'deleted', 'restored', 'relinked', 'unrelinked', 'skipped', 'archived', 'bindfail'].forEach(function (k) {
      if (url.searchParams.has(k)) { url.searchParams.delete(k); cut = true; }
    });
    if (cut && window.history && history.replaceState) history.replaceState(null, '', url.pathname + url.search + url.hash);
  } catch (err) { /* не критично */ }
  $$('.banner').forEach(function (b) {
    if (!b.querySelector('form,a,button')) setTimeout(function () { b.classList.add('gone'); }, 4500);
  });

  /* ── смена ника у строки таба (страница дня): окно выбора игрока ─── */
  (function () {
    var holder = $('[data-bind-url]');
    if (!holder) return;
    var players = null;                       // список игроков подгружается при первом открытии окна
    var load = function () {
      if (players) return Promise.resolve(players);
      return fetch(holder.getAttribute('data-players-url'), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function (list) { players = list; return list; });
    };
    var dlg = document.createElement('dialog');
    dlg.className = 'pick';
    dlg.innerHTML = '<div class="pick-head"><h3>Кто это на табе?</h3><p></p>' +
      '<input type="search" placeholder="Поиск по нику…" aria-label="Поиск игрока" autocomplete="off"></div>' +
      '<div class="pick-list"></div><div class="pick-foot"><button type="button" data-no>Отмена</button></div>';
    document.body.appendChild(dlg);
    var note = $('p', dlg), search = $('input', dlg), list = $('.pick-list', dlg), cur = null;

    var draw = function () {
      if (!players) return;
      var needle = search.value.trim().toLowerCase(), group = null, first = null;
      list.textContent = '';
      (players || []).forEach(function (p) {
        if (needle && p.nick.toLowerCase().indexOf(needle) < 0) return;
        if (p.group !== group) {
          group = p.group;
          var title = document.createElement('div');
          title.className = 'pick-grp';
          title.textContent = group;
          list.appendChild(title);
        }
        var mine = !!cur && String(p.id) === cur.pid, b = document.createElement('button');
        b.type = 'button';
        b.className = 'pick-item' + (mine ? ' cur' : '');
        b.dataset.id = p.id;
        b.textContent = p.nick + (mine ? ' ✓' : '');
        if (!first) { first = b; b.classList.add('first'); }
        list.appendChild(b);
      });
      if (!first) {
        var none = document.createElement('div');
        none.className = 'pick-empty';
        none.textContent = 'Никого не нашли';
        list.appendChild(none);
      }
    };

    var choose = function (id) {
      if (!cur || !id) return;
      var f = document.createElement('form');
      f.method = 'post';
      f.action = holder.getAttribute('data-bind-url');
      [['row', cur.row], ['player', id]].forEach(function (pair) {
        var i = document.createElement('input');
        i.type = 'hidden'; i.name = pair[0]; i.value = pair[1];
        f.appendChild(i);
      });
      document.body.appendChild(f);
      f.submit();
    };

    document.addEventListener('click', function (e) {
      var b = e.target.closest('button.match');
      if (!b) return;
      e.preventDefault();
      cur = { row: b.dataset.row, pid: b.dataset.pid || '' };
      var many = Number(b.dataset.same || 1);
      note.textContent = 'Ник на табе: «' + b.dataset.nick + '». ' +
        (many > 1 ? 'Заменится во всех табах дня (строк: ' + many + ').' : 'Выберите игрока из списка.');
      search.value = '';
      list.innerHTML = '<div class="pick-empty">Загрузка…</div>';
      dlg.showModal();
      search.focus();
      load().then(draw).catch(function () {
        dlg.close();
        window.toast('Не удалось загрузить список игроков', 'err');
      });
    });
    list.addEventListener('click', function (e) {
      var b = e.target.closest('.pick-item');
      if (b) choose(b.dataset.id);
    });
    search.addEventListener('input', draw);
    search.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      var first = $('.pick-item', list);
      if (first) choose(first.dataset.id);
    });
    $('[data-no]', dlg).addEventListener('click', function () { dlg.close(); });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
  })();

  /* ── панель сохранения на странице дня ───────────────────────────── */
  var form = $('form.day-edit'), bar = $('.savebar');
  if (form && bar) {
    var count = function () {
      var n = 0;
      $$('input', form).forEach(function (i) { if (i.type !== 'hidden' && i.value !== i.defaultValue) n++; });
      return n;
    };
    var update = function () {
      var n = count();
      bar.classList.toggle('show', n > 0);
      $('.cnt', bar).textContent = n ? 'Изменено: ' + n : '';
    };
    form.addEventListener('input', update);
    var cancel = $('[data-cancel]', bar);
    if (cancel) cancel.addEventListener('click', function () { form.reset(); update(); });
  }
})();
