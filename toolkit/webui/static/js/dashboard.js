(function () {
  const root = document.getElementById('overview-metrics');
  if (!root) return;

  const histories = {};
  for (const name of ['cpu', 'memory', 'disk']) {
    try {
      histories[name] = JSON.parse(root.dataset[`${name}History`] || '[]');
    } catch (_) {
      histories[name] = [];
    }
  }

  function numeric(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : null;
  }

  function updateMetric(name, value) {
    const amount = numeric(value);
    const label = document.getElementById(`metric-${name}`);
    const fill = document.getElementById(`metric-bar-${name}`);
    const track = fill?.parentElement;
    if (label) label.textContent = amount === null ? 'Unavailable' : `${Math.round(amount)}%`;
    if (fill) fill.style.width = `${amount || 0}%`;
    if (!track) return;
    if (amount === null) {
      track.removeAttribute('aria-valuenow');
      track.setAttribute('aria-valuetext', 'Unavailable');
    } else {
      track.removeAttribute('aria-valuetext');
      track.setAttribute('aria-valuenow', String(amount));
    }
  }

  function drawHistory(name, points) {
    const canvas = document.getElementById(`${name}-history`);
    if (!canvas) return;
    const values = points.map((point) => numeric(point[1])).filter((value) => value !== null);
    const empty = document.getElementById(`${name}-history-empty`);
    canvas.hidden = values.length === 0;
    if (empty) empty.hidden = values.length !== 0;
    if (!values.length) return;

    const width = Math.max(320, Math.round(canvas.getBoundingClientRect().width));
    const height = 128;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext('2d');
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);

    context.strokeStyle = '#1c1c1f';
    context.lineWidth = 1;
    for (const value of [25, 50, 75]) {
      const y = height - (value / 100) * height;
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }

    context.strokeStyle = '#22b8cf';
    context.lineWidth = 2;
    context.lineJoin = 'round';
    context.beginPath();
    values.forEach((value, index) => {
      const x = values.length === 1 ? width : (index / (values.length - 1)) * width;
      const y = height - (value / 100) * height;
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
  }

  function updateText(id, value) {
    const element = document.getElementById(id);
    if (element && value !== null && value !== undefined) element.textContent = String(value);
  }

  async function refresh() {
    if (document.hidden || refresh.inFlight) return;
    refresh.inFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/dashboard/metrics', {
        credentials: 'same-origin',
        signal: controller.signal,
      });
      if (!response.ok) return;
      const metrics = await response.json();
      updateMetric('cpu', metrics.cpu);
      updateMetric('memory', metrics.mem);
      updateMetric('disk', metrics.disk);
      updateText('summary-targets-up', metrics.targets_up ?? 0);
      updateText('summary-targets-down', metrics.targets_down ?? 0);
      for (const name of ['cpu', 'memory', 'disk']) {
        const data = metrics[`${name}HistoryData`];
        if (Array.isArray(data)) histories[name] = data;
        drawHistory(name, histories[name]);
      }
    } catch (_) {
      // The current snapshot remains visible when the controller is temporarily unavailable.
    } finally {
      window.clearTimeout(timeout);
      refresh.inFlight = false;
    }
  }
  refresh.inFlight = false;

  updateMetric('cpu', root.dataset.cpu);
  updateMetric('memory', root.dataset.memory);
  updateMetric('disk', root.dataset.disk);
  for (const name of ['cpu', 'memory', 'disk']) drawHistory(name, histories[name]);

  let resizeTimer;
  window.addEventListener('resize', () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      for (const name of ['cpu', 'memory', 'disk']) drawHistory(name, histories[name]);
    }, 120);
  });
  window.setInterval(refresh, 30000);
})();
