/* Shared Chart.js defaults + formatters, applied once so every chart on
   every page looks like one system instead of each template inventing its
   own axis/tooltip/gridline treatment. Per the design brief: minimal
   gridlines, no border box, no permanent point markers, short currency
   labels ("£10k" not "£10,000.00"), clean tooltips. */
/* One colour per concept, on every page (mirrors the --series-* tokens). */
const UN = {
  revenue: '#0e9f6e', costs: '#58645f', profit: '#3f6489', capex: '#a9b3ae',
  brand: '#0e9f6e', brandSoft: 'rgba(14,159,110,0.12)', grid: 'rgba(20,32,28,0.06)',
};

(function () {
  if (typeof Chart === 'undefined') return;

  Chart.defaults.font.family = "'Inter', sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.color = '#7a8781';
  Chart.defaults.borderColor = 'rgba(20,32,28,0.06)';
  Chart.defaults.maintainAspectRatio = false;

  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 4;
  Chart.defaults.elements.point.hitRadius = 8;
  Chart.defaults.elements.line.borderWidth = 2;
  Chart.defaults.elements.bar.borderRadius = 3;
  Chart.defaults.elements.bar.borderSkipped = false;

  Chart.defaults.plugins.legend.position = 'top';
  Chart.defaults.plugins.legend.align = 'start';
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Chart.defaults.plugins.legend.labels.boxHeight = 8;
  Chart.defaults.plugins.legend.labels.pointStyleWidth = 8;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.padding = 16;

  Chart.defaults.plugins.tooltip.backgroundColor = '#14201c';
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 6;
  Chart.defaults.plugins.tooltip.titleFont = { weight: '600', size: 12 };
  Chart.defaults.plugins.tooltip.bodyFont = { size: 12 };
  Chart.defaults.plugins.tooltip.boxPadding = 4;
  Chart.defaults.plugins.tooltip.callbacks = { title: (items) => items.length ? monthLabel(items[0].label) : '' };
  Chart.defaults.plugins.tooltip.displayColors = true;
})();

function moneyShort(v) {
  if (v === null || v === undefined) return '';
  const abs = Math.abs(v);
  if (abs >= 1000) return (v < 0 ? '-' : '') + '£' + (abs / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return '£' + Math.round(v);
}

function moneyFull(v) {
  return '£' + Number(v).toLocaleString('en-GB', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function pctShort(v) {
  return Math.round(v) + '%';
}

/* No vertical gridlines, one faint horizontal set -- the "minimal
   gridlines, no unnecessary border" rule applied to a Chart.js scale pair. */
const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function monthLabel(l) {
  const m = /^(\d{4})-(\d{2})$/.exec(l);
  return m ? MONTHS_SHORT[+m[2] - 1] + " '" + m[1].slice(2) : l;
}
function monthTicks() {
  return { callback: function (v) { return monthLabel(this.getLabelForValue(v)); }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 };
}

function quietScales(yTickFormatter, extra) {
  return Object.assign({
    x: { grid: { display: false }, border: { display: false }, ticks: monthTicks() },
    y: {
      beginAtZero: true,
      grid: { color: UN.grid },
      border: { display: false },
      ticks: { callback: yTickFormatter, maxTicksLimit: 6 },
    },
  }, extra || {});
}

/* One right-hand drawer for every "inspect this record" interaction
   (transactions today; bookings, documents, targets follow). Rows or links
   use hx-get / hx-target="#drawer"; this opens it with a skeleton at once
   and keeps the user on the page they were on. */
function openDrawer() {
  document.getElementById('drawer-backdrop').classList.add('open');
  document.getElementById('drawer').classList.add('open');
}
function closeDrawer() {
  document.getElementById('drawer-backdrop').classList.remove('open');
  document.getElementById('drawer').classList.remove('open');
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
document.addEventListener('DOMContentLoaded', () => {
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    const t = e.detail.target;
    if (t && t.id === 'drawer') {
      t.innerHTML = '<div class="skeleton" style="height:34px;width:60%"></div><div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line" style="width:70%"></div>';
      openDrawer();
    }
  });
});
