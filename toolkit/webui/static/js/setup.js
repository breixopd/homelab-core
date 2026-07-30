const settingInputs = Array.from(document.querySelectorAll('[name^="service_setting__"]'));
const secretRows = Array.from(document.querySelectorAll('.setup-secret-row'));

function settingValue(input) {
  return String(input.type === 'checkbox' ? input.checked : input.value).toLowerCase();
}

function syncSetupSecrets() {
  secretRows.forEach((row) => {
    const conditions = Array.from(row.querySelectorAll('[data-condition-field]'));
    const active = conditions.every((condition) => {
      const input = document.getElementById(condition.dataset.conditionField);
      const values = condition.dataset.conditionValues.toLowerCase().split('|');
      return input && values.includes(settingValue(input));
    });
    const input = row.querySelector('input');
    row.hidden = !active;
    input.disabled = !active;
    input.required = active && row.querySelector('.required-mark') !== null;
  });
}

settingInputs.forEach((input) => input.addEventListener('change', syncSetupSecrets));
syncSetupSecrets();
