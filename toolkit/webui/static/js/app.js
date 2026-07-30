/** Shared UI helpers */
const PEOPLE_IDENTITY_ERROR_STATUSES = new Set([400, 409, 429, 503]);
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

function injectCsrf(form) {
  if ((form.method || 'get').toLowerCase() === 'get' || form.querySelector('input[name="csrf_token"]')) return;
  const input = document.createElement('input');
  input.type = 'hidden';
  input.name = 'csrf_token';
  input.value = csrfToken;
  form.appendChild(input);
}

document.querySelectorAll('form').forEach(injectCsrf);
document.addEventListener('submit', (event) => {
  const form = event.target.closest('form');
  const message = event.submitter?.dataset.confirm || form?.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
document.body.addEventListener('htmx:afterSwap', (event) => {
  event.detail.target?.querySelectorAll('form').forEach(injectCsrf);
});

const sidebar = document.getElementById('sidebar');
const navToggle = document.querySelector('[data-nav-toggle]');

function setNavigationOpen(open) {
  if (!sidebar || !navToggle) return;
  sidebar.classList.toggle('open', open);
  navToggle.setAttribute('aria-expanded', String(open));
  document.body.classList.toggle('nav-open', open);
}

navToggle?.addEventListener('click', () => setNavigationOpen(!sidebar?.classList.contains('open')));
document.querySelector('[data-nav-dismiss]')?.addEventListener('click', () => setNavigationOpen(false));

function disclosureTarget(control) {
  const targetId = control.dataset.disclosureToggle || control.dataset.rowPanel;
  return targetId ? document.getElementById(targetId) : null;
}

function controlForDisclosure(targetId) {
  return Array.from(document.querySelectorAll('[data-disclosure-toggle], [data-row-panel]'))
    .find((control) => (control.dataset.disclosureToggle || control.dataset.rowPanel) === targetId);
}

function setDisclosure(control, open, { focusPanel = false, restoreFocus = false } = {}) {
  const target = disclosureTarget(control);
  if (!target) return;
  if (open && control.dataset.disclosureGroup) {
    document.querySelectorAll(`[data-disclosure-group="${control.dataset.disclosureGroup}"]`).forEach((candidate) => {
      if (candidate !== control) setDisclosure(candidate, false);
    });
  }
  target.hidden = !open;
  control.setAttribute('aria-expanded', String(open));
  if (open && focusPanel) target.querySelector('input:not([type="hidden"]), select, textarea, button')?.focus();
  if (!open && restoreFocus) control.focus();
}

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  setNavigationOpen(false);
  const expandedControls = Array.from(
    document.querySelectorAll('[data-disclosure-toggle][aria-expanded="true"], [data-row-panel][aria-expanded="true"]'),
  );
  const activeControl = expandedControls.find((control) => {
    const target = disclosureTarget(control);
    return target && target.contains(document.activeElement);
  }) || expandedControls.at(-1);
  if (activeControl) setDisclosure(activeControl, false, { restoreFocus: true });
});

document.querySelectorAll('[data-disclosure-toggle]').forEach((control) => {
  control.addEventListener('click', () => {
    const target = disclosureTarget(control);
    if (!target) return;
    const opening = target.hidden;
    setDisclosure(control, opening, { focusPanel: true });
  });
});

document.querySelectorAll('[data-disclosure-close]').forEach((control) => {
  control.addEventListener('click', () => {
    const target = document.getElementById(control.dataset.disclosureClose);
    if (!target) return;
    const opener = controlForDisclosure(target.id);
    if (opener) setDisclosure(opener, false, { restoreFocus: true });
  });
});

document.querySelectorAll('[data-row-panel]').forEach((control) => {
  control.addEventListener('click', () => {
    const target = disclosureTarget(control);
    if (!target) return;
    const opening = target.hidden;

    document.querySelectorAll('.people-detail-row:not([hidden])').forEach((row) => {
      const rowControl = controlForDisclosure(row.id);
      if (rowControl) setDisclosure(rowControl, false);
    });

    setDisclosure(control, opening, { focusPanel: true });
  });
});

function tableControls(body, selector, dataKey) {
  return Array.from(document.querySelectorAll(selector)).filter((control) => control.dataset[dataKey] === body.id);
}

function applyTableView(body) {
  const search = tableControls(body, '[data-table-filter]', 'tableFilter')[0];
  const query = search?.value.trim().toLocaleLowerCase() || '';
  const selected = body.dataset.filterValue || body.dataset.filterDefault || 'all';
  const limit = Number.parseInt(body.dataset.filterLimit || '0', 10);
  const expanded = body.dataset.filterExpanded === 'true';
  let matching = 0;
  let shown = 0;

  body.querySelectorAll('[data-filter-row]').forEach((row) => {
    const queryMatches = !query || row.textContent.toLocaleLowerCase().includes(query);
    const optionMatches = selected === 'all' || row.dataset.filterValue === selected;
    const matches = queryMatches && optionMatches;
    if (matches) matching += 1;
    const visible = matches && (expanded || query || limit <= 0 || matching <= limit);
    row.hidden = !visible;
    if (visible) shown += 1;
  });

  const empty = body.querySelector('[data-filter-empty]');
  if (empty) empty.hidden = matching !== 0;
  tableControls(body, '[data-filter-count]', 'filterCount').forEach((count) => {
    count.textContent = shown === matching ? `${shown} shown` : `${shown} of ${matching} shown`;
  });
  tableControls(body, '[data-table-option]', 'tableOption').forEach((control) => {
    const active = control.dataset.filterValue === selected;
    control.classList.toggle('active', active);
    control.setAttribute('aria-pressed', String(active));
  });
  tableControls(body, '[data-table-expand]', 'tableExpand').forEach((control) => {
    control.hidden = Boolean(query) || limit <= 0 || matching <= limit;
    control.textContent = expanded ? 'Show fewer' : `Show all ${matching}`;
  });
}

function initializeTableViews(root = document) {
  const controls = [
    ...root.querySelectorAll('[data-table-filter]'),
    ...root.querySelectorAll('[data-table-option]'),
    ...root.querySelectorAll('[data-table-expand]'),
  ];
  controls.forEach((control) => {
    if (control.dataset.tableViewBound === 'true') return;
    control.dataset.tableViewBound = 'true';
    if (control.matches('[data-table-filter]')) {
      control.addEventListener('input', () => {
        const body = document.getElementById(control.dataset.tableFilter);
        if (body) applyTableView(body);
      });
      control.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || !control.value) return;
        control.value = '';
        const body = document.getElementById(control.dataset.tableFilter);
        if (body) applyTableView(body);
      });
      return;
    }
    if (control.matches('[data-table-option]')) {
      control.addEventListener('click', () => {
        const body = document.getElementById(control.dataset.tableOption);
        if (!body) return;
        body.dataset.filterValue = control.dataset.filterValue;
        body.dataset.filterExpanded = 'false';
        applyTableView(body);
      });
      return;
    }
    control.addEventListener('click', () => {
      const body = document.getElementById(control.dataset.tableExpand);
      if (!body) return;
      body.dataset.filterExpanded = String(body.dataset.filterExpanded !== 'true');
      applyTableView(body);
    });
  });

  const bodies = new Set(controls.map((control) => {
    const id = control.dataset.tableFilter || control.dataset.tableOption || control.dataset.tableExpand;
    return id ? document.getElementById(id) : null;
  }).filter(Boolean));
  bodies.forEach(applyTableView);
}

initializeTableViews();
document.body.addEventListener('htmx:afterSwap', (event) => initializeTableViews(event.detail.target));

document.body.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-copy-value]');
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.dataset.copyValue || '');
    const original = button.textContent;
    button.textContent = 'Copied';
    window.setTimeout(() => { button.textContent = original; }, 1200);
  } catch (_) {
    button.textContent = 'Copy failed';
  }
});

function updateMachineKind(control) {
  const form = control.closest('form');
  if (!form) return;
  const isVm = control.value === 'vm';
  form.querySelectorAll('[data-vm-fields]').forEach((section) => {
    section.hidden = !isVm;
    section.querySelectorAll('input, select, textarea').forEach((input) => { input.disabled = !isVm; });
  });
}

document.querySelectorAll('[data-machine-kind]').forEach((control) => {
  updateMachineKind(control);
  control.addEventListener('change', () => updateMachineKind(control));
});

document.querySelectorAll('[data-section-jump]').forEach((control) => {
  control.addEventListener('change', () => {
    const target = document.getElementById(control.value);
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    window.history.replaceState(null, '', `#${target.id}`);
  });
});

function routePeopleIdentityResponse(event) {
  const { detail } = event;
  if (detail.target?.id !== 'identity-job-panel') return;
  const errorRegion = document.getElementById('identity-error');
  if (!errorRegion) return;

  if (!PEOPLE_IDENTITY_ERROR_STATUSES.has(detail.xhr?.status)) {
    if (detail.xhr?.status >= 200 && detail.xhr.status < 300) errorRegion.replaceChildren();
    return;
  }

  detail.target = errorRegion;
  detail.shouldSwap = true;
  detail.isError = false;
}

document.body.addEventListener('htmx:beforeSwap', routePeopleIdentityResponse);
document.body.addEventListener('htmx:configRequest', (event) => {
  event.detail.credentials = 'same-origin';
  event.detail.headers['X-CSRF-Token'] = csrfToken;
});
document.body.addEventListener('htmx:beforeRequest', (event) => {
  const trigger = event.detail.elt?.getAttribute('hx-trigger') || '';
  if (document.hidden && trigger.includes('every')) event.preventDefault();
});
document.body.addEventListener('htmx:responseError', () => {
  const el = document.getElementById('spinner');
  if (el) el.setAttribute('aria-label', 'Request failed');
});
document.body.addEventListener('htmx:afterRequest', () => {
  const el = document.getElementById('spinner');
  if (el) el.setAttribute('aria-label', 'Request in progress');
});

window.HomelabIdentity = {
  attach(jobId) {
    if (window.HomelabDeploy) window.HomelabDeploy.attach(jobId);
  },
};
