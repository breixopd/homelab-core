from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_registry_owner_does_not_pull_through_its_own_mirror() -> None:
    playbook = (ROOT / "automation/ansible/playbooks/setup-docker-registry.yml").read_text(encoding="utf-8")

    assert "homelab_node_id != registry_mirror_node" in playbook
    assert "registry_mirror_url" in playbook
    assert "service_ips['registry-mirror']" not in playbook
    assert "service_ips['adguard']" not in playbook
    assert '"registry-mirrors": {{ registry_mirrors | to_json }}' in playbook


def test_dns_client_uses_manifest_selected_provider() -> None:
    template = (ROOT / "automation/ansible/roles/dns_client/templates/resolved.conf.j2").read_text(encoding="utf-8")

    assert "DNS={{ dns_service_ip }}" in template
    assert "service_ips['adguard']" not in template
