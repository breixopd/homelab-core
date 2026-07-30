from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from toolkit.core.images.catalog import custom_images

ROOT = Path(__file__).resolve().parents[4]


def test_production_ui_is_separated_from_host_authority() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.example.yml").read_text())
    services = compose["services"]
    controller = services["homelab-controller"]
    ui = services["homelab-ui"]

    expected_image = "${HOMELAB_UI_IMAGE:?run homelab-toolkit generate}"
    assert controller["image"] == expected_image
    assert ui["image"] == expected_image
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in controller["volumes"]
    assert all("docker.sock" not in volume for volume in ui["volumes"])
    assert controller["security_opt"] == ["no-new-privileges:true"]
    assert ui["security_opt"] == ["no-new-privileges:true"]
    assert "controller-socket:/run/homelab-controller" in controller["volumes"]
    assert "controller-payload-key:/run/secrets/homelab-controller" in controller["volumes"]
    assert all("controller-payload-key" not in volume for volume in ui["volumes"])
    assert "controller-socket:/run/homelab-controller:ro" in ui["volumes"]
    assert "homelab-ui-state:/var/lib/homelab-ui" in ui["volumes"]
    assert all("./:/app/repo" not in volume for volume in ui["volumes"])
    assert "${INSTALL_ROOT:-.}/config.yaml:/app/repo/config.yaml:ro" in ui["volumes"]
    assert ui["working_dir"] == "/opt/homelab-framework"
    assert ui["environment"]["PYTHONPATH"] == "/opt/homelab-framework"
    assert ui["depends_on"]["homelab-controller"]["condition"] == "service_healthy"
    assert ui["user"] == "10001:10001"
    assert controller["environment"]["HOMELAB_CONTROLLER_UI_GID"] == "10001"
    assert controller["pids_limit"] == 4096
    assert controller["environment"]["HOMELAB_NODE"] == "${HOMELAB_NODE}"
    assert ui["environment"]["HOMELAB_NODE"] == "${HOMELAB_NODE}"
    assert controller["environment"]["HOMELAB_CONTROLLER_PAYLOAD_KEY_FILE"] == (
        "/run/secrets/homelab-controller/payload.key"
    )
    assert ui["environment"]["HOMELAB_CONTROLLER_SOCKET"].endswith("controller.sock")
    assert ui["environment"]["HOMELAB_CONTROLLER_ROLE"] == "ui"
    assert ui["environment"]["HOMELAB_CONTROLLER_TOKEN_FILE"].endswith("ui.token")
    assert ui["environment"]["WEBUI_SESSION_SECRET_FILE"] == "/var/lib/homelab-ui/webui-secret"
    assert ui["environment"]["WEBUI_SECURE_COOKIES"] == "true"
    assert ui["environment"]["HOMELAB_TRUSTED_PROXY_CIDRS"] == "${CADDY_EDGE_IP}/32"
    assert ui["environment"]["HOMELAB_TRUST_CONTAINER_GATEWAY"] == "true"
    assert "SOPS_AGE_KEY_FILE" not in ui["environment"]
    healthcheck = " ".join(controller["healthcheck"]["test"])
    assert controller["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "curl -fsS --unix-socket /run/homelab-controller/controller.sock" in healthcheck
    assert "http://localhost/v1/health" in healthcheck
    assert "status" in healthcheck and "ok" in healthcheck
    assert "python" not in healthcheck


def test_service_application_owns_the_controller_topology() -> None:
    application = yaml.safe_load((ROOT / "toolkit" / "services" / "homelab-ui" / "compose.yaml").read_text())
    controller = application["services"]["homelab-controller"]
    ui = application["services"]["homelab-ui"]

    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in controller["volumes"]
    assert all("/var/run/docker.sock" not in volume for volume in ui["volumes"])
    assert "controller-socket:/run/homelab-controller" in controller["volumes"]
    assert "controller-payload-key:/run/secrets/homelab-controller" in controller["volumes"]
    assert "${INSTALL_ROOT:-.}:/app/repo" in controller["volumes"]
    assert "${INSTALL_ROOT:-.}/config.yaml:/app/repo/config.yaml:ro" in ui["volumes"]
    assert controller["pids_limit"] == 4096


def test_deploy_sync_keeps_private_automation_key_on_control_node_only() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/tasks/sync-from-controller.yml").read_text())
    by_name = {task["name"]: task for task in tasks}

    public = by_name["Sync automation public key to managed node"]
    private = by_name["Sync automation private key to control node"]
    remove = by_name["Remove automation private key from workload node"]
    revision = by_name["Read controller revision for guest parity stamp"]
    assert public["ansible.builtin.copy"]["mode"] == "0644"
    assert private["when"] == "homelab_node_id == control_node"
    assert private["ansible.builtin.copy"]["mode"] == "0600"
    assert private["no_log"] is True
    assert remove["when"] == "homelab_node_id != control_node"
    assert revision["delegate_to"] == "localhost"
    assert revision["connection"] == "local"
    assert revision["become"] is False
    assert revision["vars"]["ansible_become"] is False
    assert revision["ansible.builtin.command"]["argv"][0] == "{{ ansible_playbook_python }}"
    assert "controller_commit_sha" in revision["ansible.builtin.command"]["argv"][2]


def test_host_setup_authorizes_controller_identity_without_private_key_copy() -> None:
    playbook_path = ROOT / "automation/ansible/host-setup.yml"
    playbook = playbook_path.read_text(encoding="utf-8")
    host_setup = yaml.safe_load(playbook)[0]
    requirements = yaml.safe_load((ROOT / "automation/ansible/requirements.yml").read_text(encoding="utf-8"))

    local_key_check = next(
        task
        for task in host_setup["tasks"]
        if task["name"] == "Check controller automation public key is available locally"
    )
    assert local_key_check["delegate_to"] == "localhost"
    assert local_key_check["connection"] == "local"
    assert local_key_check["become"] is False
    assert local_key_check["vars"]["ansible_become"] is False

    assert "ansible.posix.authorized_key" in playbook
    assert "homelab_admin_ed25519.pub" in playbook
    assert 'homelab_admin_ed25519"' not in playbook
    assert 'command="/usr/bin/false"' in playbook
    assert "'restrict', 'port-forwarding'" in playbook
    assert "'permitopen=\"' + item.value.address" in playbook
    assert "item.value.ssh_port | default(22)" in playbook
    assert "machine_specs | default({})" in playbook
    assert "machine_specs | default({}) | length > 0" in playbook
    assert "private key" in playbook.lower()
    assert any(item["name"] == "ansible.posix" for item in requirements["collections"])


def test_tofu_leaves_root_only_lxc_features_to_host_reconciliation() -> None:
    tofu = (ROOT / "infrastructure/main.tf").read_text(encoding="utf-8")
    container = tofu.split('resource "proxmox_virtual_environment_container" "machine"', 1)[1]
    feature_block = container.split("features {", 1)[1].split("}", 1)[0]

    assert "nesting = each.value.nesting" in feature_block
    assert "keyctl" not in feature_block
    assert "fuse" not in feature_block

    reconcile = (ROOT / "automation/ansible/playbooks/configure-lxc-features.yml").read_text(encoding="utf-8")
    assert "fuse={{ 1 if item.item.value.fuse else 0 }}" in reconcile
    assert "keyctl={{ 1 if item.item.value.keyctl else 0 }}" in reconcile


def test_deploy_sync_keeps_age_identity_on_control_node_only() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/tasks/sync-from-controller.yml").read_text())
    by_name = {task["name"]: task for task in tasks}

    sync = by_name["Sync age identity to the control node"]
    remove = by_name["Remove controller age identities from workload nodes"]
    copy = sync["ansible.builtin.copy"]

    assert copy["src"] == "{{ homelab_controller_root }}/keys/age.key"
    assert copy["dest"] == "{{ repo_dest | default('/opt/homelab') }}/keys/age.key"
    assert copy["mode"] == "0600"
    assert sync["when"] == "homelab_node_id == control_node"
    assert sync["no_log"] is True
    assert remove["when"] == "homelab_node_id != control_node"


def test_deploy_sync_keeps_recovery_artifacts_on_controller_and_role_secrets_scoped() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/tasks/sync-from-controller.yml").read_text())
    by_name = {task["name"]: task for task in tasks}

    models = by_name["Sync retained Compose models from controller"]
    assert "homelab_node_id == control_node" in models["when"][1]

    role_hooks = by_name["Sync role-scoped hook secrets from controller"]["ansible.builtin.copy"]
    assert role_hooks["dest"].endswith("/generated/{{ homelab_node_id }}/.hooks.env")

    recovery_hooks = by_name["Sync recovery hook bundles to the control node"]
    assert recovery_hooks["when"][0] == "homelab_node_id == control_node"
    assert recovery_hooks["no_log"] is True
    assert recovery_hooks["ansible.builtin.copy"]["mode"] == "0600"
    assert recovery_hooks["ansible.builtin.copy"]["dest"].endswith("/generated/bundles/{{ item }}/.hooks.env")

    cleanup = by_name["Remove non-local hook secrets from workload nodes"]
    assert "homelab_node_id != control_node" in cleanup["when"]
    assert cleanup["ansible.builtin.file"]["path"].endswith("/generated/{{ item }}/.hooks.env")


def test_deploy_sync_uses_manifest_owned_generated_artifacts() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/tasks/sync-from-controller.yml").read_text())
    by_name = {task["name"]: task for task in tasks}
    task_names = [task["name"] for task in tasks]

    assert "Sync generated tree from Ansible controller" not in by_name
    cleanup = by_name["Remove generated entries not owned by the workload node"]
    assert "homelab_node_id != control_node" in cleanup["when"]

    select = by_name["Select node-scoped service artifacts for batched transfer"]
    assert select["loop"] == "{{ service_generated_artifacts | default([]) }}"
    assert "homelab_node_id in item.nodes" in select["when"][1]
    assert select["no_log"] is True
    assert task_names.index(select["name"]) < task_names.index("Discover generated entries retained on workload nodes")

    cleanup_when = " ".join(cleanup["when"])
    assert "homelab_authorized_service_artifacts" in cleanup_when
    assert "map(attribute='path')" in cleanup_when
    assert "regex_replace', '/.*$', ''" in cleanup_when

    sync = by_name["Transfer node-scoped service artifacts in one protected archive"]
    assert "homelab_authorized_service_artifacts" in sync["when"]
    nested = {task["name"]: task for task in sync["block"]}
    archive = nested["Build node-authorized service artifact archive on controller"]
    archive_argv = archive["ansible.builtin.command"]["argv"]
    assert "--owner=0" in archive_argv
    assert "--numeric-owner" in archive_argv
    assert "--files-from=-" not in archive_argv
    assert "homelab_authorized_service_artifacts" in archive_argv
    assert "map(attribute='path')" in archive_argv
    assert "map('regex_replace', '^', 'generated/')" in archive_argv
    assert "\\\\1" not in archive_argv
    assert "stdin" not in archive["ansible.builtin.command"]
    assert archive["no_log"] is False
    assert archive["vars"] == {"ansible_become": False, "ansible_connection": "local"}
    extract = nested["Extract node-authorized service artifacts on managed node"]
    assert extract["ansible.builtin.unarchive"]["owner"] == "root"
    assert extract["no_log"] is True
    stale = by_name["Remove stale or non-local generated service artifacts"]
    assert "homelab_node_id not in item.nodes" in stale["when"][0]


def test_deploy_sync_scopes_manifest_owned_config_sources() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/tasks/sync-from-controller.yml").read_text())
    by_name = {task["name"]: task for task in tasks}

    build = by_name["Add non-local and disabled config paths to rsync exclusions"]
    assert build["loop"] == "{{ service_config_sources | default([]) }}"
    assert "homelab_node_id != control_node" in build["when"][0]
    assert "item.node != homelab_node_id" in build["when"][1]
    sync = by_name["Sync config tree from Ansible controller"]
    assert sync["vars"]["sync_rsync_opts"] == "{{ service_config_rsync_opts }}"
    assert "--exclude=/" in build["ansible.builtin.set_fact"]["service_config_rsync_opts"]
    cleanup = by_name["Remove stale or non-local service-owned config from managed node"]
    assert cleanup["loop"] == "{{ service_config_sources | default([]) }}"
    assert cleanup["ansible.builtin.file"]["path"].endswith("/{{ item.path }}")


def test_redis_copies_owner_only_configuration_into_private_tmpfs() -> None:
    application = yaml.safe_load((ROOT / "toolkit" / "services" / "redis" / "compose.yaml").read_text())
    redis = application["services"]["redis"]

    assert "/run/redis" in redis["tmpfs"]
    assert redis["entrypoint"] == ["/bin/sh", "-ec"]
    assert "cp /run/redis-source/redis.conf /run/redis/redis.conf" in redis["command"][0]
    assert "chown redis:redis /run/redis/redis.conf" in redis["command"][0]
    assert "chmod" not in redis["command"][0]
    assert "docker-entrypoint.sh redis-server /run/redis/redis.conf" in redis["command"][0]


def test_recovery_applies_controller_rendered_artifacts_without_guest_regeneration() -> None:
    playbook = (ROOT / "automation" / "ansible" / "playbooks" / "deploy-recover.yml").read_text()

    assert "Regenerate configs" not in playbook
    assert "-m toolkit.cli" not in playbook.split("- name: Staggered compose up", 1)[0]


def test_recovery_and_security_wait_for_ssh_before_gathering_facts() -> None:
    for relative in (
        "automation/ansible/playbooks/deploy-recover.yml",
        "automation/ansible/playbooks/deploy-security-agents.yml",
    ):
        play = yaml.safe_load((ROOT / relative).read_text())[0]
        assert play["gather_facts"] is False
        actions = [next((key for key in task if key.startswith("ansible.builtin.")), "") for task in play["pre_tasks"]]
        assert actions[:2] == ["ansible.builtin.wait_for_connection", "ansible.builtin.setup"]


def test_guest_setup_reconnects_hosts_after_best_effort_storage() -> None:
    plays = yaml.safe_load((ROOT / "automation/ansible/guest-setup.yml").read_text())
    reconnect = next(play for play in plays if play.get("name") == "Reconnect guest nodes after optional storage")

    assert reconnect["gather_facts"] is False
    assert [task["name"] for task in reconnect["tasks"]] == [
        "Clear stale unreachable state after optional storage",
        "Wait for guest SSH after optional storage",
    ]
    assert reconnect["tasks"][1]["ansible.builtin.wait_for_connection"]["timeout"] == 180


def test_guest_setup_dispatches_service_owned_guest_hooks() -> None:
    guest_setup = ROOT / "automation/ansible/guest-setup.yml"
    source = guest_setup.read_text()
    plays = yaml.safe_load(source)
    integration_play = next(
        play for play in plays if play.get("name") == "Apply manifest-owned guest integrations to managed nodes"
    )
    integration_tasks = integration_play["tasks"]
    dispatch = next(
        task for task in integration_tasks if task["name"] == "Apply manifest-selected service guest task file"
    )
    assert dispatch["ansible.builtin.include_tasks"] == "{{ homelab_controller_root }}/{{ service_task_file }}"
    assert dispatch["loop_control"]["loop_var"] == "service_task_file"
    assert dispatch["loop"] == "{{ service_guest_task_files | default([]) }}"
    assert "wazuh_manager" not in source
    assert "security_agent" not in source
    assert "ldap_client" not in source
    assert "vpn_client" not in source
    assert "service_nodes['adguard']" not in source

    final_play = next(
        play for play in plays if play.get("name") == "Run final post-start hooks after guest configuration"
    )
    final_dispatch = next(
        task for task in final_play["tasks"] if task["name"] == "Apply manifest-selected final guest task files"
    )
    assert final_dispatch["loop"] == "{{ service_guest_final_task_files | default([]) }}"
    assert "service_nodes['adguard']" not in source

    for relative in (
        "toolkit/services/wazuh-indexer/ansible/guest.yml",
        "toolkit/services/wazuh-dashboard/ansible/guest.yml",
        "toolkit/services/lldap/ansible/guest.yml",
        "toolkit/services/headscale/ansible/guest.yml",
        "toolkit/services/adguard/ansible/guest-final.yml",
    ):
        assert (ROOT / relative).is_file(), relative


def test_registry_restart_handler_waits_for_guest_ssh_recovery() -> None:
    plays = yaml.safe_load((ROOT / "automation/ansible/playbooks/setup-docker-registry.yml").read_text())
    handlers = plays[0]["handlers"]

    assert {handler["name"] for handler in handlers} >= {
        "Restart Docker",
        "Reset SSH connection after Docker restart",
        "Wait for SSH after Docker restart",
    }
    wait = next(handler for handler in handlers if handler["name"] == "Wait for SSH after Docker restart")
    assert wait["listen"] == "restart docker"
    assert wait["ansible.builtin.wait_for_connection"]["timeout"] == 180


def test_ansible_automation_uses_fact_namespace_without_deprecated_top_level_facts() -> None:
    files = [
        ROOT / "automation/ansible/playbooks/bootstrap-lxc.yml",
        ROOT / "automation/ansible/roles/vpn_client/tasks/main.yml",
        ROOT / "automation/ansible/roles/komodo_periphery/tasks/main.yml",
        ROOT / "automation/ansible/roles/crowdsec_agent/tasks/main.yml",
    ]

    for path in files:
        content = path.read_text()
        assert "ansible_architecture" not in content
        assert "ansible_distribution_release" not in content


def test_recovery_playbook_leaves_hooks_and_verification_to_controller_workflow() -> None:
    playbook = yaml.safe_load((ROOT / "automation/ansible/playbooks/deploy-recover.yml").read_text())
    task_names = {task.get("name") for play in playbook for task in play.get("tasks", [])}
    source = (ROOT / "automation/ansible/playbooks/deploy-recover.yml").read_text()

    assert "Run post-start hooks" not in task_names
    assert "Verify deployment" not in task_names
    assert "Apply manifest-selected service recovery tasks" in task_names
    assert "Restore Docker published-port policy" in task_names
    assert "ldap_client" not in source
    assert "security_agent" not in source
    assert "wazuh_manager" not in source
    assert "service_nodes['wazuh-indexer']" not in source


def test_standalone_service_playbooks_dispatch_manifest_owned_hooks() -> None:
    expectations = {
        "automation/ansible/playbooks/install-wazuh-manager.yml": (
            "service_manager_task_files",
            "wazuh-indexer",
            "wazuh_manager",
        ),
        "automation/ansible/playbooks/deploy-security-agents.yml": (
            "service_security_task_files",
            "security_agent",
            "wazuh",
        ),
        "automation/ansible/playbooks/sync-ldap-clients.yml": (
            "service_sync_task_files",
            "ldap_client",
            "lldap",
        ),
    }
    for relative, (variable, role, service) in expectations.items():
        source = (ROOT / relative).read_text()
        assert f'loop: "{{{{ {variable} | default([]) }}}}"' in source, relative
        assert role not in source, relative
        assert service not in source, relative


def test_security_agent_does_not_install_or_repair_wazuh_manager() -> None:
    tasks = (ROOT / "automation/ansible/roles/security_agent/tasks/main.yml").read_text()

    assert "apt-get install -y wazuh-manager" not in tasks
    assert "Reinstall wazuh-manager" not in tasks


def test_wazuh_manager_uses_supported_analysisd_limit_within_systemd_ceiling() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/roles/wazuh_manager/tasks/main.yml").read_text())
    by_name = {task["name"]: task for task in tasks}

    task = by_name["Align Wazuh analysisd file descriptor limit with its systemd unit"]
    config = task["ansible.builtin.lineinfile"]
    assert config["path"] == "/var/ossec/etc/local_internal_options.conf"
    assert config["regexp"] == r"^analysisd\.rlimit_nofile="
    assert config["line"] == "analysisd.rlimit_nofile=65536"
    assert config["mode"] == "0640"
    assert task["notify"] == "restart wazuh-manager"


def test_guest_security_owns_unattended_upgrade_policy_and_timers() -> None:
    tasks = yaml.safe_load((ROOT / "automation/ansible/roles/security_agent/tasks/main.yml").read_text())
    by_name = {task["name"]: task for task in tasks}

    policy = by_name["Configure unattended operating-system upgrades"]["ansible.builtin.copy"]
    assert policy["dest"] == "/etc/apt/apt.conf.d/52homelab-unattended-upgrades"
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in policy["content"]
    assert 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";' in policy["content"]
    timers = by_name["Enable operating-system update timers"]["ansible.builtin.systemd"]
    assert timers["name"] == "{{ item }}"
    assert by_name["Enable operating-system update timers"]["loop"] == [
        "apt-daily.timer",
        "apt-daily-upgrade.timer",
    ]


def test_guest_automation_resolves_service_placement_without_default_machine_names() -> None:
    bootstrap = (ROOT / "automation/ansible/playbooks/bootstrap-lxc.yml").read_text()
    deploy = (ROOT / "automation/ansible/playbooks/deploy-server-toolkit.yml").read_text()
    security_defaults = (ROOT / "automation/ansible/roles/security_agent/defaults/main.yml").read_text()
    security_tasks = (ROOT / "automation/ansible/roles/security_agent/tasks/main.yml").read_text()
    sync = (ROOT / "automation/ansible/tasks/sync-from-controller.yml").read_text()

    combined = "\n".join((bootstrap, deploy, security_defaults, security_tasks))
    assert "groups.infra" not in combined
    assert "groups.get('infra'" not in combined
    assert "'infra' in group_names" not in combined
    assert "'infra' not in group_names" not in combined
    assert "\"dns\": {{ docker_dns_servers | default(['1.1.1.1']) | to_json }}" in bootstrap
    assert "homelab_node_id == ingress_node" in deploy
    assert "service_nodes['wazuh-indexer']" in security_tasks
    assert "service_generated_artifacts | default([])" in sync
    assert "Sync generated tree from Ansible controller" not in sync
    assert '"--exclude=infra/compose.yaml"' not in sync


def test_ui_image_context_excludes_private_and_unstable_local_state() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert {
        ".git",
        ".venv",
        ".runtime",
        ".homelab-state",
        ".superpowers",
        "**/__pycache__",
        "**/*.py[cod]",
        "config.yaml",
        "config.local.yaml",
        "config/kopia",
        "keys",
        "secrets*.yaml",
        "ssh",
        "**/.terraform",
        "**/*.tfstate",
        "**/*.tfstate.*",
        "**/*.tfplan",
        "infrastructure/generated.auto.tfvars",
        "infrastructure/terraform.tfvars",
        "automation/ansible/group_vars/all.yml",
        "automation/ansible/group_vars/generated*.yml",
        "*.pem",
        "*.agekey",
    }.issubset(patterns)


def test_repository_ignores_local_credentials_and_agent_state() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert {
        ".agents/",
        ".codex/",
        ".superpowers/",
        ".env.*",
        "*.env.*",
        "*.key",
        "*.pem",
        "*.p12",
        "*.pfx",
        "*.token",
        "*.credentials",
    }.issubset(patterns)


def test_service_file_binds_are_rooted_at_install_root() -> None:
    for compose_path in (ROOT / "toolkit" / "services").glob("*/compose.yaml"):
        application = yaml.safe_load(compose_path.read_text()) or {}
        for service in (application.get("services") or {}).values():
            for volume in service.get("volumes") or []:
                if isinstance(volume, str):
                    assert not volume.startswith(("./config/", "./generated/")), (compose_path, volume)


def test_caddy_egress_network_reserves_static_address() -> None:
    platform = yaml.safe_load((ROOT / "stacks" / "platform.yaml").read_text())
    ipam = platform["networks"]["caddy-egress"]["ipam"]["config"][0]

    assert ipam["ip_range"] == "${EDGE_DYNAMIC_RANGE:-172.31.250.128/25}"
    assert "aux_addresses" not in ipam


def test_authelia_uses_current_reset_password_secret_environment() -> None:
    application = yaml.safe_load((ROOT / "toolkit" / "services" / "authelia" / "compose.yaml").read_text())
    environment = application["services"]["authelia"]["environment"]

    assert environment["AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET"] == "${AUTHELIA_JWT_SECRET}"
    assert "AUTHELIA_JWT_SECRET" not in environment
    assert "AUTHELIA_OIDC_HMAC_SECRET" not in environment
    assert environment["PUID"] == "1000"
    assert environment["PGID"] == "1000"


def test_wazuh_healthcheck_uses_mounted_admin_key() -> None:
    application = yaml.safe_load((ROOT / "toolkit" / "services" / "wazuh-indexer" / "compose.yaml").read_text())
    indexer = application["services"]["wazuh-indexer"]
    mounted_targets = {volume.rsplit(":", 2)[1] for volume in indexer["volumes"]}
    healthcheck = " ".join(indexer["healthcheck"]["test"])

    assert "/usr/share/wazuh-indexer/config/certs/admin-key.pem" in mounted_targets
    assert "/usr/share/wazuh-indexer/config/certs/admin-key.pem" in healthcheck


def test_wazuh_dashboard_preserves_image_initialized_config() -> None:
    application = yaml.safe_load((ROOT / "toolkit/services/wazuh-dashboard/compose.yaml").read_text())
    volumes = application["services"]["wazuh-dashboard"]["volumes"]

    assert "wazuh-dashboard-config:/usr/share/wazuh-dashboard/data/wazuh/config" in volumes
    assert "wazuh-dashboard-custom:/usr/share/wazuh-dashboard/plugins/wazuh/public/assets/custom" in volumes
    assert not any("WAZUH_DASHBOARD_DATA_SOURCE" in volume for volume in volumes)


def test_ci_builds_toolkit_image_from_repository_context() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    pull_request_image_job = workflow.split("  publish-images:", 1)[0]
    image = next(image for image in custom_images(ROOT) if image.name == "homelab-ui")

    assert image.context == "."
    assert image.dockerfile == "toolkit/Dockerfile"
    assert image.repository == "homelab-toolkit"
    assert image.platforms == ("linux/amd64", "linux/arm64")
    assert 'images build --image "$IMAGE_NAME"' in workflow
    assert "matrix.image.platforms" in workflow
    assert "matrix.image.dockerfile" in workflow
    assert "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8 # v4.2.0" in workflow
    assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c # v4.2.0" in workflow
    assert "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a # v7.3.0" in workflow
    assert "Build all declared platforms without publishing" in pull_request_image_job
    assert "platforms: ${{ matrix.image.platforms }}" in pull_request_image_job
    assert "push: false" in pull_request_image_job


def test_controller_service_reconciliation_preserves_the_running_controller() -> None:
    playbook_path = ROOT / "automation" / "ansible" / "playbooks" / "deploy-server-toolkit.yml"
    playbook = playbook_path.read_text()

    assert "--setenv=HOMELAB_PRESERVE_CONTROLLER=\"{{ homelab_preserve_controller | default('0') }}\"" in playbook
    assert "HOMELAB_PRESERVE_CONTROLLER: \"{{ homelab_preserve_controller | default('0') }}\"" in playbook
    assert playbook.count("homelab_preserve_controller | default(false) | bool") == 2
    assert playbook.count("and homelab_node_id == control_node") >= 2

    def nested_tasks(value):
        if isinstance(value, dict):
            if "name" in value:
                yield value
            for child in value.values():
                yield from nested_tasks(child)
        elif isinstance(value, list):
            for child in value:
                yield from nested_tasks(child)

    wait = next(
        task
        for task in nested_tasks(yaml.safe_load(playbook_path.read_text()))
        if task["name"] == "Wait for staggered compose to finish"
    )
    assert wait["ansible.builtin.command"]["argv"][0] == "{{ ansible_playbook_python }}"
    assert "{{ homelab_controller_root }}/.venv/bin/python3" not in wait["ansible.builtin.command"]["argv"]

    verify_hooks = next(
        task
        for task in nested_tasks(yaml.safe_load(playbook_path.read_text()))
        if task["name"] == "Verify post-start hooks for this VM role"
    )
    assert verify_hooks["ansible.builtin.command"]["argv"][0] == "{{ ansible_playbook_python }}"
    assert "{{ homelab_controller_root }}/.venv/bin/python3" not in playbook

    sync_tasks = yaml.safe_load(
        (ROOT / "automation" / "ansible" / "tasks" / "sync-toolkit-from-controller.yml").read_text()
    )
    tarball_sync = next(task for task in sync_tasks if task["name"] == "Sync toolkit via tarball fallback")
    assert tarball_sync["ansible.builtin.command"]["argv"][0] == "{{ ansible_playbook_python }}"


def test_toolkit_image_downloads_verified_tools_for_each_supported_architecture() -> None:
    dockerfile = (ROOT / "toolkit" / "Dockerfile").read_text()

    assert "bind9-dnsutils" in dockerfile
    assert "rsync" in dockerfile
    assert "ARG TARGETARCH" in dockerfile
    assert "COMPOSE_SHA256_ARM64" in dockerfile
    assert "OPENTOFU_SHA256_ARM64" in dockerfile
    assert "SOPS_SHA256_ARM64" in dockerfile
    assert "AGE_SHA256_ARM64" in dockerfile
    assert "docker-compose-linux-${compose_arch}" in dockerfile
    assert "tofu_${OPENTOFU_VERSION}_linux_${TARGETARCH}.zip" in dockerfile
    assert "sops-v${SOPS_VERSION}.linux.${TARGETARCH}" in dockerfile
    assert "age-v${AGE_VERSION}-linux-${TARGETARCH}.tar.gz" in dockerfile


def test_installer_keeps_local_controller_credential_out_of_ui_bind_mount() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text()

    assert "controller-data:/var/lib/homelab-controller" in installer
    assert "controller-payload-key:/run/secrets/homelab-controller" in installer
    assert "HOMELAB_CONTROLLER_DB: /var/lib/homelab-controller/controller.db" in installer
    assert "HOMELAB_CONTROLLER_LOCAL_TOKEN_FILE: /var/lib/homelab-controller/local.token" in installer
    assert "HOMELAB_CONTROLLER_PAYLOAD_KEY_FILE: /run/secrets/homelab-controller/payload.key" in installer
    assert 'HOMELAB_CONTROLLER_LOCAL_TOKEN_FILE: "${CONTAINER_HOMELAB_ROOT}' not in installer
    toolkit_block = installer.split("  toolkit:", 1)[1].split("volumes:\n", 1)[0]
    assert "${INSTALL_ROOT}:${CONTAINER_HOMELAB_ROOT}" not in toolkit_block
    assert "SSH_VOLUME_LINE" not in toolkit_block
    assert "AGENT_VOLUME_LINE" not in toolkit_block
    assert "cap_add" not in toolkit_block


def test_ci_publishes_immutable_toolkit_commit_tag() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "sha-${GITHUB_SHA}" in workflow
    assert "GITHUB_REF_TYPE" in workflow
    assert "packages: write" in workflow
    assert "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6 # v4.2.0" in workflow
    assert "Unable to resolve the published image digest" not in workflow
    assert "push_output=" not in workflow
    assert "RepoDigests" not in workflow
    assert "subject-digest: ${{ steps.push.outputs.digest }}" in workflow


def test_ci_publishes_images_only_after_the_full_release_gate() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    needs = set(workflow["jobs"]["publish-images"]["needs"])

    assert needs >= {
        "image-plan",
        "test",
        "coverage",
        "e2e",
        "source-validation",
        "tofu-validate",
        "integration",
        "gitleaks",
    }


def test_ci_generation_uses_ephemeral_operator_identity() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    fixture = (ROOT / "scripts" / "prepare-ci-config.sh").read_text(encoding="utf-8")

    assert workflow.count("./scripts/prepare-ci-config.sh") == 2
    assert "config set dns.public_ip=192.0.2.10" in workflow
    assert "ssh-keygen" in fixture
    assert "dns.public_ip=192.0.2.10" in fixture
    assert "proxmox.ssh_public_key=" in fixture
    assert "proxmox.ssh.key_file=" in fixture
    assert "age-keygen" in fixture
    assert "SOPS_AGE_KEY_FILE" in fixture
    assert "GITHUB_ENV" in fixture
    assert "RUNNER_TEMP" in fixture
    assert "config set" in fixture


def test_ci_discovers_custom_images_from_service_manifests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "images list --ci --json" in workflow
    assert "fromJSON(needs.image-plan.outputs.images)" in workflow
    assert "matrix.image.repository" in workflow
    assert "IMAGE_NAME: ${{ matrix.image.name }}" in workflow
    assert 'case "${{ matrix.image.name }}"' not in workflow
    for image in custom_images(ROOT):
        assert f"name: {image.name}" not in workflow


def test_lldap_owns_an_immutable_ldap_client_image() -> None:
    manifest = yaml.safe_load((ROOT / "toolkit" / "services" / "lldap" / "service.yaml").read_text())
    compose = yaml.safe_load((ROOT / "toolkit" / "services" / "lldap" / "compose.yaml").read_text())
    dockerfile = (ROOT / "toolkit" / "services" / "lldap" / "image" / "Dockerfile").read_text()

    assert manifest["image_build"]["env_var"] == "HOMELAB_LLDAP_IMAGE"
    assert compose["services"]["lldap"]["build"]["context"] == "./toolkit/services/lldap/image"
    assert compose["services"]["lldap"]["image"] == "${HOMELAB_LLDAP_IMAGE:?run homelab-toolkit generate}"
    assert manifest["data_specs"][0]["host_uid"] == 1000
    assert manifest["data_specs"][0]["host_gid"] == 1000
    assert "ldap-utils=" in dockerfile
    assert "lldap/lldap:" in dockerfile and "@sha256:" in dockerfile


def test_ci_dependency_audits_are_blocking() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "pip-audit -r ${{ matrix.image.context }}/requirements.txt || true" not in workflow


def test_local_automation_uses_locked_project_tools() -> None:
    makefile = (ROOT / "Makefile").read_text()
    local_ci = (ROOT / "scripts" / "local-ci.sh").read_text()
    installer = (ROOT / "scripts" / "install.sh").read_text()

    assert "pip install" not in makefile
    assert "pip install" not in local_ci
    assert "sync --locked" in makefile
    assert "uv sync --locked" in local_ci
    assert "uv sync --locked --no-dev --no-install-project" in installer
    assert "uv run --no-sync python -m toolkit.cli" in installer
    assert 'run_phase "ansible-lint"' in local_ci
    assert "ansible-lint --project-dir automation/ansible" in local_ci


def test_local_ci_multi_command_phases_fail_closed() -> None:
    local_ci = (ROOT / "scripts" / "local-ci.sh").read_text()

    assert "ruff check toolkit/ tests/ scripts/ || return 1" in local_ci
    assert 'uv build --wheel --out-dir "$out" || return 1' in local_ci
    assert 'homelab-toolkit --root "$REPO_ROOT" generate || return 1' in local_ci
    assert "docker build -t homelab-toolkit:local-ci -f toolkit/Dockerfile . || return 1" in local_ci
    assert 'images build --image "$name" --registry local --tag ci || return 1' in local_ci
    assert 'images test --image "$name" --registry local --tag ci || return 1' in local_ci
    assert 'images audit --image "$name" || return 1' in local_ci


def test_guest_toolkit_install_is_locked_and_fail_closed() -> None:
    tasks = (ROOT / "automation" / "ansible" / "tasks" / "ensure-guest-toolkit-venv.yml").read_text()
    dockerfile = (ROOT / "toolkit" / "Dockerfile").read_text()
    uv_image = next(
        line.split(" AS ", 1)[0].removeprefix("FROM ") for line in dockerfile.splitlines() if line.endswith(" AS uv")
    )

    assert "uv sync --locked --no-dev" in tasks
    assert uv_image in tasks
    assert "docker create" in tasks
    assert "docker cp" in tasks
    assert "docker rm" in tasks
    assert "entrypoint cat" not in tasks
    assert "ansible.builtin.pip" not in tasks
    assert "fallback" not in tasks.lower()
    assert "failed_when: false" not in tasks


def test_renovate_owns_python_and_lockfile_with_pep621_manager() -> None:
    renovate = (ROOT / "renovate.json").read_text(encoding="utf-8")

    assert '"pep621"' in renovate
    assert '"pip_requirements"' not in renovate
    assert '"matchPackagePatterns"' not in renovate
    assert '"fileMatch"' not in renovate


def test_renovate_discovers_manifest_owned_release_tags_and_digests() -> None:
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    manager = next(item for item in renovate["customManagers"] if item["customType"] == "regex")
    pattern = re.compile(manager["matchStrings"][0].replace("(?<", "(?P<"))
    releases = {
        match.group("depName"): (match.group("currentValue"), match.group("currentDigest"))
        for manifest in (ROOT / "toolkit" / "services").glob("*/service.yaml")
        if (match := pattern.search(manifest.read_text(encoding="utf-8"))) is not None
    }

    assert set(releases) == {
        "docker.io/prom/node-exporter",
        "ghcr.io/breixopd/media-cache",
        "ghcr.io/breixopd/music-sync",
    }
    assert all(re.fullmatch(r"v?[0-9][A-Za-z0-9_.-]*", version) for version, _digest in releases.values())
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) for _version, digest in releases.values())


def test_renovate_tracks_shared_uv_and_checksummed_tool_pins() -> None:
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = {item["description"]: item for item in renovate["customManagers"]}
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "toolkit" / "Dockerfile").read_text(encoding="utf-8")

    uv_manager = managers["Keep the CI uv version aligned with the toolkit image."]
    uv_version = re.search(uv_manager["matchStrings"][0].replace("(?<", "(?P<"), ci)
    assert uv_version is not None
    assert f"ghcr.io/astral-sh/uv:{uv_version.group('currentValue')}@sha256:" in dockerfile

    tool_manager = managers["Update checksummed toolkit binaries from official releases."]
    tool_pattern = re.compile(tool_manager["matchStrings"][0].replace("(?<", "(?P<"))
    tools = {match.group("depName"): match.group("currentValue") for match in tool_pattern.finditer(dockerfile)}
    assert set(tools) == {"docker/compose", "opentofu/opentofu", "getsops/sops", "FiloSottile/age"}
    assert all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version) for version in tools.values())
