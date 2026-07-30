window.HomelabDeploy = (function () {
  const active = new Set();

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value || '—';
  }

  function attach(jobId) {
    if (active.has(jobId)) return;

    const panel = Array.from(document.querySelectorAll('[data-job-id]'))
      .find((item) => item.dataset.jobId === jobId);
    if (!panel) return;
    active.add(jobId);
    const url = panel.dataset.streamUrl;
    const logEl = document.getElementById(`log-${jobId}`);
    const statusEl = document.getElementById(`job-status-${jobId}`);
    let source = null;
    let lastEventId = '';

    function remember(ev) {
      if (ev.lastEventId) lastEventId = ev.lastEventId;
    }

    function connect(retries = 0) {
      const resumeUrl = new URL(url, window.location.href);
      if (lastEventId) resumeUrl.searchParams.set('after', lastEventId);
      source = new EventSource(resumeUrl.toString(), { withCredentials: true });

      source.addEventListener('log', (ev) => {
        remember(ev);
        if (!logEl) return;
        const line = ev.data;
        if (line.startsWith('▶ ')) return;
        logEl.textContent += line + '\n';
        logEl.scrollTop = logEl.scrollHeight;
      });

      source.addEventListener('progress', (ev) => {
        remember(ev);
        try {
          const info = JSON.parse(ev.data);
          if (info.percent) {
            setText(`progress-percent-${jobId}`, info.percent + '%');
            const bar = document.getElementById(`progress-bar-${jobId}`);
            if (bar) {
              const percent = Math.min(100, parseInt(info.percent, 10) || 0);
              bar.style.width = percent + '%';
              bar.parentElement?.setAttribute('aria-valuenow', String(percent));
            }
          }
          if (info.step) setText(`progress-step-${jobId}`, info.step);
          if (info.node) setText(`progress-node-${jobId}`, info.node);
          const taskWave = info.compose_wave || info.ansible_task || '';
          if (taskWave) setText(`progress-task-${jobId}`, taskWave);
          if (info.log_file) setText(`progress-log-${jobId}`, info.log_file);
          if (info.detail) setText(`progress-detail-${jobId}`, info.detail);
        } catch (_) { /* ignore */ }
      });

      source.addEventListener('step', (ev) => {
        remember(ev);
        try {
          const { step, status } = JSON.parse(ev.data);
          const li = Array.from(document.querySelectorAll('#step-list li'))
            .find((item) => item.dataset.step === step);
          if (!li) return;
          const icon = li.querySelector('.step-marker');
          if (!icon) return;
          icon.className = 'step-marker ' + status;
        } catch (_) { /* ignore */ }
      });

      source.addEventListener('done', (ev) => {
        remember(ev);
        source.close();
        active.delete(jobId);
        let payload = { ok: false };
        try { payload = JSON.parse(ev.data); } catch (_) { /* invalid event is failed */ }
        if (statusEl) {
          const partial = payload.partial === true;
          statusEl.textContent = payload.ok ? 'succeeded' : partial ? 'partial failure' : 'failed';
          statusEl.classList.remove('running', 'ok', 'warn', 'critical');
          statusEl.classList.add(payload.ok ? 'ok' : partial ? 'warn' : 'critical');
        }
        const cancel = panel.querySelector('[data-job-cancel]');
        if (cancel) cancel.hidden = true;
        if (payload.message && logEl) logEl.textContent += '\n' + payload.message + '\n';
      });

      source.addEventListener('error', () => {
        source.close();
        if (retries < 3) {
          const nextRetries = retries + 1;
          if (logEl) logEl.textContent += `\nConnection lost - retrying (${nextRetries}/3)...\n`;
          setTimeout(() => connect(nextRetries), 2000 * nextRetries);
        } else {
          active.delete(jobId);
          if (statusEl) {
            statusEl.textContent = 'error';
            statusEl.classList.remove('running');
            statusEl.classList.add('critical');
          }
        }
      });
    }

    connect();
  }

  function attachWithin(root) {
    root.querySelectorAll('[data-job-id]').forEach((panel) => attach(panel.dataset.jobId));
  }

  return { attach, attachWithin };
})();

window.HomelabDeploy.attachWithin(document);
document.body.addEventListener('htmx:afterSwap', (event) => {
  window.HomelabDeploy.attachWithin(event.detail.target);
});
