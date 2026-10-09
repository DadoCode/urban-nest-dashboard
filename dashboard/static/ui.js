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

  /* ---------- upload progress overlay: reassurance during the real, synchronous extraction wait ---------- */
  (function () {
    const overlay = document.getElementById('upload-overlay');
    const stageEl = document.getElementById('upload-overlay-stage');
    if (!overlay || !stageEl) return;
    const stages = ['Uploading…', 'Reading the document…', 'Extracting line items…', 'Checking for duplicates…', 'Almost done…'];
    document.querySelectorAll('form.upload-form').forEach(form => {
      form.addEventListener('submit', (e) => {
        if (e.defaultPrevented || !form.checkValidity()) return;
        overlay.classList.add('open');
        stageEl.textContent = stages[0];
        let i = 0;
        setInterval(() => { i = Math.min(i + 1, stages.length - 1); stageEl.textContent = stages[i]; }, 1400);
        form.querySelectorAll('button[type="submit"]').forEach(b => { b.disabled = true; });
      });
    });
  })();

  /* ---------- keyboard access for clickable rows ---------- */
  /* <tr class="clickable"> (transaction/vendor/category/document/booking
     rows) only ever worked with a mouse -- a <tr> isn't focusable and has
     no keyboard handler, so Tab skips it and Enter does nothing. This
     makes it a real button for keyboard/screen-reader use without
     changing how it looks or works with a mouse. */
  function enableRowKeyboardAccess(root) {
    root.querySelectorAll('tr.clickable:not([tabindex])').forEach(row => {
      row.setAttribute('tabindex', '0');
      row.setAttribute('role', 'button');
      row.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        row.click();
      });
    });
  }
  document.addEventListener('DOMContentLoaded', () => enableRowKeyboardAccess(document));
  document.addEventListener('htmx:afterSwap', (e) => enableRowKeyboardAccess(e.target));

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

/* ---------- small shared behaviours ---------- */
(function () {
  // "All metrics": reveals the secondary rows of a comparison table
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-toggle-rows]');
    if (!b) return;
    const t = document.getElementById(b.dataset.toggleRows);
    if (!t) return;
    const on = t.classList.toggle('show-all');
    b.setAttribute('aria-expanded', on ? 'true' : 'false');
    b.textContent = on ? 'Fewer metrics' : 'All metrics';
  });
  // a horizontal scroller that has more to show gets a fade at its edge (nav strip, tab bar, wide tables)
  const cue = (el) => {
    const f = () => el.classList.toggle('has-more', el.scrollWidth - el.clientWidth - el.scrollLeft > 6);
    f(); el.addEventListener('scroll', f, { passive: true }); window.addEventListener('resize', f);
  };
  const init = () => document.querySelectorAll('.tabs, .sidebar, .card.flush').forEach(cue);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();

/* ---------- info controls (the ⓘ next to a metric): hover, keyboard focus, click and tap all show the same short definition ---------- */
(function () {
  let pop = null, pinned = null;
  const textOf = (el) => el.getAttribute('data-info') || el.getAttribute('aria-label') || el.getAttribute('title') || '';
  const prep = (el) => {
    if (el.hasAttribute('title')) { el.setAttribute('data-info', el.getAttribute('title')); el.removeAttribute('title'); }   // one tooltip, not the native one on top
    if (!el.hasAttribute('role')) el.setAttribute('role', 'button');
    if (!el.hasAttribute('tabindex')) el.tabIndex = 0;
    if (!el.hasAttribute('aria-expanded')) el.setAttribute('aria-expanded', 'false');
  };
  const hide = () => { if (pop) { pop.remove(); pop = null; } document.querySelectorAll('.info-dot[aria-expanded="true"]').forEach((d) => d.setAttribute('aria-expanded', 'false')); };
  const show = (el) => {
    hide();
    prep(el);
    pop = document.createElement('div');
    pop.className = 'info-pop';
    pop.setAttribute('role', 'tooltip');
    pop.textContent = textOf(el);
    document.body.appendChild(pop);
    el.setAttribute('aria-expanded', 'true');
    const r = el.getBoundingClientRect(), w = Math.min(280, window.innerWidth - 24);
    pop.style.width = w + 'px';
    pop.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left + r.width / 2 - w / 2)) + 'px';
    const below = r.bottom + 8, h = pop.offsetHeight;
    pop.style.top = (below + h > window.innerHeight - 8 && r.top - h - 8 > 8 ? r.top - h - 8 : below) + 'px';
  };
  const dot = (e) => e.target.closest && e.target.closest('.info-dot');
  document.addEventListener('mouseover', (e) => { const d = dot(e); if (d && !pinned) show(d); });
  document.addEventListener('mouseout', (e) => { if (dot(e) && !pinned) hide(); });
  document.addEventListener('focusin', (e) => { const d = dot(e); if (d && !pinned) show(d); });
  document.addEventListener('focusout', (e) => { if (dot(e) && !pinned) hide(); });
  document.addEventListener('click', (e) => {
    const d = dot(e);
    if (d) {
      e.preventDefault();                                   // an ⓘ inside a linked tile or header must not follow the link
      if (pinned === d) { pinned = null; hide(); } else { pinned = d; show(d); }
    } else if (pinned || pop) { pinned = null; hide(); }
  });
  document.addEventListener('keydown', (e) => {
    const d = dot(e);
    if (d && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); d.click(); }
    if (e.key === 'Escape' && (pinned || pop)) { pinned = null; hide(); }
  });
  window.addEventListener('scroll', () => { if (pop && !pinned) hide(); }, { passive: true });
})();
