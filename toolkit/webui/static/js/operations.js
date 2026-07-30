window.HomelabOperations = (function () {
  const active = new Set();

  function attach(jobId) {
    if (active.has(jobId)) return;

    const panel = document.querySelector(`[data-job-id="${jobId}"]`);
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
        logEl.textContent += ev.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
      });

      source.addEventListener('done', (ev) => {
        remember(ev);
        source.close();
        active.delete(jobId);
        let payload = { ok: false };
        try { payload = JSON.parse(ev.data); } catch (_) { /* invalid event is failed */ }
        if (statusEl) {
          statusEl.textContent = payload.ok ? 'succeeded' : 'failed';
          statusEl.classList.remove('running');
          statusEl.classList.add(payload.ok ? 'ok' : 'critical');
        }
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

  function syncManagedHostForm(form) {
    const kind = form.querySelector('input[name="kind"]:checked')?.value || 'fleet';
    const fleet = kind === 'fleet';
    form.querySelectorAll('[data-fleet-only-field]').forEach((field) => {
      field.hidden = !fleet;
      field.querySelectorAll('input, select, textarea').forEach((control) => { control.disabled = !fleet; });
    });
    form.querySelectorAll('[data-fleet-only-choice]').forEach((choice) => {
      const control = choice.querySelector('input');
      choice.hidden = !fleet;
      if (control) {
        control.disabled = !fleet;
        if (!fleet) control.checked = false;
      }
    });
    form.querySelectorAll('[data-host-integration]').forEach((field) => {
      const integration = field.dataset.hostIntegration;
      const toggle = form.querySelector(`[data-service-choice="${integration}"]`);
      const enabled = Boolean(toggle?.checked);
      field.hidden = !enabled;
      field.querySelectorAll('input').forEach((input) => {
        input.disabled = !enabled;
        input.required = enabled && input.hasAttribute('data-required-when-enabled');
      });
    });
  }

  document.querySelectorAll('[data-host-form]').forEach((form) => {
    syncManagedHostForm(form);
    form.addEventListener('change', (event) => {
      if (event.target.matches('input[name="kind"], [data-service-choice]')) syncManagedHostForm(form);
    });
  });

  return { attach, syncManagedHostForm };
})();
