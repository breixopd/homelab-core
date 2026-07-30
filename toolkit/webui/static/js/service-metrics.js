(function () {
  function draw(chart) {
    const canvas = chart.querySelector('canvas');
    if (!canvas) return;
    let points = [];
    try {
      points = JSON.parse(chart.dataset.series || '[]');
    } catch (_) {
      points = [];
    }
    const values = points
      .map((point) => Number(Array.isArray(point) ? point[1] : NaN))
      .filter((value) => Number.isFinite(value) && value >= 0);
    if (!values.length) {
      canvas.hidden = true;
      return;
    }

    const width = Math.max(260, Math.round(canvas.getBoundingClientRect().width));
    const height = 128;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext('2d');
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);

    const observedPeak = Math.max(...values);
    const ceiling = chart.dataset.unit === 'percent'
      ? Math.max(10, Math.min(100, Math.ceil(observedPeak / 10) * 10))
      : Math.max(1, observedPeak * 1.1);

    context.strokeStyle = '#1c1c1f';
    context.lineWidth = 1;
    for (const fraction of [0.25, 0.5, 0.75]) {
      const y = height - fraction * height;
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
      const y = height - Math.min(1, value / ceiling) * height;
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
  }

  function charts() {
    return Array.from(document.querySelectorAll('[data-service-history]'));
  }

  function drawAll() {
    charts().forEach(draw);
  }

  drawAll();
  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target?.id !== 'service-observability') return;
    event.detail.target.setAttribute('aria-busy', 'false');
    drawAll();
  });
  document.body.addEventListener('htmx:responseError', (event) => {
    if (event.detail.target?.id === 'service-observability') {
      event.detail.target.setAttribute('aria-busy', 'false');
    }
  });
  let resizeTimer;
  window.addEventListener('resize', () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(drawAll, 120);
  });
})();
