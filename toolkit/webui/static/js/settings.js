(() => {
  const preset = document.getElementById("setting-smtp-preset");
  const mode = document.getElementById("setting-smtp-mode");
  const host = document.getElementById("setting-smtp-host");
  const port = document.getElementById("setting-smtp-port");
  const username = document.getElementById("setting-smtp-user");
  const fromAddress = document.getElementById("setting-smtp-from");
  const starttls = document.querySelector('input[name="smtp_starttls"]');

  const syncGmailSender = () => {
    if (preset?.value === "gmail" && username && fromAddress) {
      fromAddress.value = username.value;
    }
  };

  preset?.addEventListener("change", () => {
    if (preset.value !== "gmail" || !mode || !host || !port || !starttls) return;
    mode.value = "external";
    host.value = "smtp.gmail.com";
    port.value = "587";
    starttls.checked = true;
    syncGmailSender();
    username?.focus();
  });
  username?.addEventListener("input", syncGmailSender);
})();
