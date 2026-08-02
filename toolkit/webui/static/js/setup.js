const settingInputs = Array.from(document.querySelectorAll('[name^="service_setting__"]'));
const secretRows = Array.from(document.querySelectorAll('.setup-secret-row'));
const deploymentModes = Array.from(document.querySelectorAll('[name="deployment_mode"]'));
const proxmoxSection = document.getElementById('proxmox-setup');

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

function syncDeploymentMode() {
  if (!proxmoxSection) return;
  const selected = deploymentModes.find((input) => input.checked)?.value || 'provision';
  const provisioning = selected === 'provision';
  proxmoxSection.hidden = !provisioning;
  proxmoxSection.querySelectorAll('input').forEach((input) => {
    input.disabled = !provisioning;
    input.required = provisioning;
  });
}

settingInputs.forEach((input) => input.addEventListener('change', syncSetupSecrets));
deploymentModes.forEach((input) => input.addEventListener('change', syncDeploymentMode));
syncSetupSecrets();
syncDeploymentMode();
