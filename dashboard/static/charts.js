/* Shared Chart.js defaults + formatters, applied once so every chart on
   every page looks like one system instead of each template inventing its
   own axis/tooltip/gridline treatment. Per the design brief: minimal
   gridlines, no border box, no permanent point markers, short currency
   labels ("£10k" not "£10,000.00"), clean tooltips. */
(function () {
  if (typeof Chart === 'undefined') return;

  Chart.defaults.font.family = "'Inter', sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.color = '#7c8d87';
  Chart.defaults.borderColor = 'rgba(15,26,23,0.06)';
  Chart.defaults.maintainAspectRatio = false;

  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 4;
  Chart.defaults.elements.point.hitRadius = 8;
  Chart.defaults.elements.line.borderWidth = 2;
  Chart.defaults.elements.bar.borderRadius = 4;
  Chart.defaults.elements.bar.borderSkipped = false;

  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.padding = 14;

  Chart.defaults.plugins.tooltip.backgroundColor = '#0f1a17';
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 8;
  Chart.defaults.plugins.tooltip.titleFont = { weight: '600', size: 12 };
  Chart.defaults.plugins.tooltip.bodyFont = { size: 12 };
  Chart.defaults.plugins.tooltip.boxPadding = 4;
  Chart.defaults.plugins.tooltip.displayColors = true;
})();

function moneyShort(v) {
  if (v === null || v === undefined) return '';
  const abs = Math.abs(v);
  if (abs >= 1000) return (v < 0 ? '-' : '') + '£' + (abs / 1000).toFixed(abs >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
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
function quietScales(yTickFormatter, extra) {
  return Object.assign({
    x: { grid: { display: false }, border: { display: false } },
    y: {
      beginAtZero: true,
      grid: { color: 'rgba(15,26,23,0.06)' },
      border: { display: false },
      ticks: { callback: yTickFormatter, maxTicksLimit: 6 },
    },
  }, extra || {});
}
