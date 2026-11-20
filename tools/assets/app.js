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
    ['saved', 'deleted', 'restored', 'relinked', 'unrelinked', 'skipped', 'archived'].forEach(function (k) {
      if (url.searchParams.has(k)) { url.searchParams.delete(k); cut = true; }
    });
    if (cut && window.history && history.replaceState) history.replaceState(null, '', url.pathname + url.search + url.hash);
  } catch (err) { /* не критично */ }
  $$('.banner').forEach(function (b) {
    if (!b.querySelector('form,a,button')) setTimeout(function () { b.classList.add('gone'); }, 4500);
  });

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
