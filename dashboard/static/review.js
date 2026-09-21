/* Document review workspace: live flags, selection + bulk actions, duplicate
   decisions, confirm summary/blocking, and the source viewer (PDF page/zoom,
   image zoom, CSV/XLSX row highlight). Server re-validates on confirm; this
   only guides the eye. */
(function () {
  const form = document.getElementById('review-form');
  const viewer = document.getElementById('viewer');

  /* ---------- source viewer ---------- */
  const view = { page: 1, zoom: 100 };
  function renderPdf() {
    if (!viewer || viewer.dataset.type !== 'pdf') return;
    const src = viewer.dataset.src + '#page=' + view.page + '&zoom=' + view.zoom + '&navpanes=0';
    viewer.innerHTML = '<iframe title="Source document" src="' + src + '"></iframe>';
    const label = document.getElementById('viewer-label');
    if (label) label.textContent = 'Page ' + view.page + ' · ' + view.zoom + '%';
  }
  function renderImage() {
    if (!viewer || viewer.dataset.type !== 'image') return;
    const img = viewer.querySelector('img');
    if (img) img.style.width = view.zoom + '%';
    const label = document.getElementById('viewer-label');
    if (label) label.textContent = view.zoom + '%';
  }
  function repaint() { renderPdf(); renderImage(); }
  function gotoPage(n) { if (viewer && viewer.dataset.type === 'pdf' && n && n !== view.page) { view.page = n; repaint(); } }
  function highlightRow(n) {
    const box = document.getElementById('preview-scroll');
    if (!box || !n) return;
    box.querySelectorAll('tr.hl').forEach(t => t.classList.remove('hl'));
    const tr = box.querySelector('tr[data-r="' + n + '"]');
    if (tr) { tr.classList.add('hl'); box.scrollTop = Math.max(0, tr.offsetTop - box.clientHeight / 3); }
  }
  document.querySelectorAll('[data-viewer]').forEach(btn => btn.addEventListener('click', () => {
    const a = btn.dataset.viewer;
    if (a === 'prev') view.page = Math.max(1, view.page - 1);
    if (a === 'next') view.page += 1;
    if (a === 'in') view.zoom = Math.min(300, view.zoom + 25);
    if (a === 'out') view.zoom = Math.max(50, view.zoom - 25);
    repaint();
  }));
  const fp = viewer && parseInt(viewer.dataset.focusPage, 10);
  if (fp) view.page = fp;
  repaint();
  const pv = document.getElementById('preview-scroll');
  if (pv && pv.dataset.focusRow) highlightRow(parseInt(pv.dataset.focusRow, 10));

  document.querySelectorAll('.src-link').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    if (a.dataset.page) gotoPage(parseInt(a.dataset.page, 10));
    if (a.dataset.srcRow) highlightRow(parseInt(a.dataset.srcRow, 10));
  }));

  if (!form) return;

  /* ---------- rows ---------- */
  const kind = form.dataset.kind;                       // 'tx' | 'res'
  const noun = kind === 'res' ? 'reservation' : 'transaction';
  const rows = Array.from(form.querySelectorAll('div[data-row]'));
  const f = (r, name) => { const el = r.querySelector('[name="' + name + '"]'); return el ? el.value : ''; };
  const isIso = s => /^\d{4}-\d{2}-\d{2}$/.test(s) && !isNaN(Date.parse(s));
  const included = r => r.querySelector('.row-include').checked;
  const hasDup = r => r.dataset.dup === '1';
  const dupChoice = r => { const c = r.querySelector('.dup-choice:checked'); return c ? c.value : ''; };

  function analyse(r) {
    const soft = [], hard = [];
    if (!f(r, 'property_id')) hard.push('Property needs review');
    if (kind === 'tx') {
      const cat = f(r, 'category');
      if (!cat || cat === 'other') soft.push('Category uncertain');
      if (!isIso(f(r, 'date'))) hard.push('Date unclear');
      else if (r.dataset.dateGuess && f(r, 'date') === r.dataset.dateGuess) soft.push('Date unclear');
      if (!(parseFloat(f(r, 'amount')) > 0)) hard.push('Amount missing');
    } else {
      const ci = f(r, 'check_in'), co = f(r, 'check_out');
      if (!isIso(ci) || !isIso(co) || co <= ci) hard.push('Dates unclear');
      if (!(parseFloat(f(r, 'net')) >= 0) || f(r, 'net') === '') hard.push('Amount missing');
    }
    const conf = r.dataset.conf === '' ? null : parseFloat(r.dataset.conf);
    if (conf !== null && conf < 0.7 && r.dataset.checked !== '1') soft.push('Low confidence — check the values');
    if (hasDup(r) && !dupChoice(r)) hard.push('Possible duplicate — choose what to do');
    return { soft, hard };
  }

  function corrected(r) {
    let orig; try { orig = JSON.parse(r.dataset.orig || '{}'); } catch (e) { return false; }
    const keys = kind === 'res' ? { check_in: 'check_in', check_out: 'check_out', gross: 'gross', fees: 'fees', net: 'net' }
                                : { vendor: 'vendor', description: 'description', amount: 'amount', category: 'category', date: 'date' };
    return Object.keys(keys).some(k => {
      const o = orig[k]; if (o === null || o === undefined) return false;
      const cur = f(r, keys[k]);
      const a = parseFloat(o), b = parseFloat(cur);
      if (!isNaN(a) && !isNaN(b) && typeof o !== 'string') return Math.abs(a - b) > 0.004;
      return String(o).trim() !== String(cur).trim();
    });
  }

  function update() {
    let n = 0, excluded = 0, fixed = 0, attention = 0, blockers = { prop: 0, amount: 0, date: 0, dup: 0 };
    rows.forEach(r => {
      const inc = included(r), a = analyse(r);
      r.classList.toggle('excluded', !inc);
      const flags = r.querySelector('.rv-flags');
      const all = inc ? a.hard.concat(a.soft) : [];
      flags.innerHTML = all.map(t => '<span class="rv-flag">⚠ ' + t + '</span>').join('');
      r.classList.toggle('has-flags', all.length > 0);
      r.querySelector('.rv-status').textContent = !inc ? '–' : (all.length ? '⚠' : '✓');
      r.querySelector('.rv-status').className = 'rv-status ' + (!inc ? 'off' : (all.length ? 'warn' : 'ok'));
      const tog = r.querySelector('.row-toggle'); if (tog) tog.textContent = inc ? 'Exclude' : 'Restore';
      if (!inc) { excluded++; return; }
      n++;
      if (all.length) attention++;
      if (corrected(r)) fixed++;
      a.hard.forEach(t => {
        if (/^Property/.test(t)) blockers.prop++; else if (/^Amount/.test(t)) blockers.amount++;
        else if (/^Date/.test(t)) blockers.date++; else if (/^Possible/.test(t)) blockers.dup++;
      });
    });
    rows.forEach(r => { if (!included(r) && corrected(r)) fixed++; });
    const btn = document.getElementById('confirm-btn');
    btn.textContent = 'Confirm ' + n + ' ' + noun + (n === 1 ? '' : 's');
    const problems = [];
    if (blockers.dup) problems.push(blockers.dup + ' possible duplicate' + (blockers.dup === 1 ? '' : 's') + ' to decide');
    if (blockers.prop) problems.push(blockers.prop + ' without a property');
    if (blockers.amount) problems.push(blockers.amount + ' without an amount');
    if (blockers.date) problems.push(blockers.date + ' with unclear dates');
    const blocked = problems.length > 0 || n === 0;
    btn.disabled = blocked;
    const parts = [];
    if (excluded) parts.push(excluded + ' excluded');
    if (fixed) parts.push(fixed + ' manually corrected');
    document.getElementById('confirm-note').innerHTML = problems.length
      ? '<span class="rv-block">' + problems.join(' · ') + '</span>'
      : (n === 0 ? 'Nothing is included yet.' : (parts.join(' · ') || 'Everything looks ready.'));
    const total = document.getElementById('rv-total');
    if (total) total.textContent = rows.length + ' line' + (rows.length === 1 ? '' : 's') + ' · ' + attention + ' need' + (attention === 1 ? 's' : '') + ' your attention';
    const sel = rows.filter(r => r.querySelector('.row-select').checked).length;
    document.getElementById('bulk-bar').hidden = sel === 0;
    document.getElementById('bulk-count').textContent = sel + ' selected';
    const all = document.getElementById('select-all'); all.checked = sel > 0 && sel === rows.length; all.indeterminate = sel > 0 && sel < rows.length;
  }

  function setExcluded(r, on) {
    r.querySelector('.row-include').checked = !on;
    const radios = r.querySelectorAll('.dup-choice');
    if (radios.length) {
      if (on) radios.forEach(x => { x.checked = x.value === 'exclude'; });
      else radios.forEach(x => { x.checked = false; });
    }
  }

  rows.forEach(r => {
    r.addEventListener('input', e => { if (e.target.name === 'amount' || e.target.name === 'net') r.dataset.checked = '1'; update(); });
    r.addEventListener('change', update);
    r.querySelectorAll('.dup-choice').forEach(x => x.addEventListener('change', () => {
      r.querySelector('.row-include').checked = x.value === 'keep' && x.checked;
      update();
    }));
    const tog = r.querySelector('.row-toggle');
    if (tog) tog.addEventListener('click', () => { setExcluded(r, included(r)); update(); });
    r.addEventListener('focusin', () => {
      if (r.dataset.page) gotoPage(parseInt(r.dataset.page, 10));
      if (r.dataset.srcRow) highlightRow(parseInt(r.dataset.srcRow, 10));
      rows.forEach(x => x.classList.toggle('focus', x === r));
    });
  });
  /* ---------- selection + bulk actions ---------- */
  document.getElementById('select-all').addEventListener('change', e => {
    rows.forEach(r => { if (r.offsetParent !== null) r.querySelector('.row-select').checked = e.target.checked; });
    update();
  });
  const selected = () => rows.filter(r => r.querySelector('.row-select').checked);
  const fire = el => el.dispatchEvent(new Event('change', { bubbles: true }));
  document.querySelectorAll('[data-bulk]').forEach(btn => btn.addEventListener('click', () => {
    const a = btn.dataset.bulk;
    selected().forEach(r => {
      if (a === 'exclude') setExcluded(r, true);
      if (a === 'restore') setExcluded(r, false);
      if (a === 'opex' || a === 'capex') { const t = r.querySelector('[name="type"]'); if (t) { t.value = a; fire(t); } }
    });
    update();
  }));
  [['bulk-property', 'property_id'], ['bulk-category', 'category']].forEach(([id, name]) => {
    const sel = document.getElementById(id); if (!sel) return;
    sel.addEventListener('change', () => {
      if (!sel.value) return;
      selected().forEach(r => { const t = r.querySelector('[name="' + name + '"]'); if (t) { t.value = sel.value; fire(t); } });
      sel.value = ''; update();
    });
  });
  document.getElementById('only-attention').addEventListener('change', e => form.classList.toggle('only-attention', e.target.checked));

  form.addEventListener('submit', e => { if (document.getElementById('confirm-btn').disabled) e.preventDefault(); });
  update();
})();
