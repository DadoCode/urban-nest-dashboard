/* Shell behaviour: navigation progress line and the ⌘K quick-jump. */
(function () {
  /* ---------- view-only accounts: show everything, allow no changes ---------- */
  function lockForms(root) {
    if (!document.body.classList.contains('viewer')) return;
    root.querySelectorAll('form[method="post"]').forEach(f => {
      f.classList.add('view-only');
      f.querySelectorAll('input, select, textarea, button').forEach(el => { el.disabled = true; });
      f.addEventListener('submit', e => e.preventDefault());
    });
  }
  document.addEventListener('DOMContentLoaded', () => lockForms(document));
  document.addEventListener('htmx:afterSwap', (e) => lockForms(e.target));

  /* ---------- navigation progress ---------- */
  const bar = document.getElementById('navbar');
  function start() { if (!bar) return; bar.style.opacity = 1; bar.style.width = '70%'; }
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || a.target === '_blank') return;
    const href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript') || a.hasAttribute('hx-get') || a.origin !== location.origin) return;
    start();
  });
  document.addEventListener('submit', (e) => { if (!e.defaultPrevented) start(); });
  window.addEventListener('pageshow', () => { if (bar) { bar.style.opacity = 0; bar.style.width = '0'; } });

  /* ---------- quick jump ---------- */
  const box = document.getElementById('palette');
  if (!box) return;
  const input = document.getElementById('palette-input');
  const list = document.getElementById('palette-list');
  const items = JSON.parse(document.getElementById('palette-data').textContent);
  let shown = [], cursor = 0;

  function render() {
    const q = input.value.trim().toLowerCase();
    shown = items.filter(i => !q || i.label.toLowerCase().includes(q) || i.hint.toLowerCase().includes(q)).slice(0, 12);
    cursor = Math.min(cursor, Math.max(0, shown.length - 1));
    list.innerHTML = shown.length ? shown.map((i, k) =>
      '<a href="' + i.url + '" class="palette-item' + (k === cursor ? ' sel' : '') + '"><span>' + i.label + '</span><span class="note">' + i.hint + '</span></a>').join('')
      : '<div class="palette-empty">Nothing matches “' + input.value + '”.</div>';
  }
  function open() { box.hidden = false; input.value = ''; cursor = 0; render(); input.focus(); }
  function close() { box.hidden = true; }
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); box.hidden ? open() : close(); return; }
    if (e.key === '/' && !typing && box.hidden) { e.preventDefault(); open(); return; }
    if (box.hidden) return;
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowDown') { e.preventDefault(); cursor = Math.min(cursor + 1, shown.length - 1); render(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); cursor = Math.max(cursor - 1, 0); render(); }
    if (e.key === 'Enter' && shown[cursor]) { start(); location.href = shown[cursor].url; }
  });
  input.addEventListener('input', () => { cursor = 0; render(); });
  box.addEventListener('click', (e) => { if (e.target === box) close(); });
  const trigger = document.getElementById('palette-open');
  if (trigger) trigger.addEventListener('click', open);
})();
