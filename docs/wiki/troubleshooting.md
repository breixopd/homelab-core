# Troubleshooting

## Services not starting

```bash
docker compose ps
docker compose logs <service> --tail 80
docker compose config --quiet
```

Use the per-role env file when debugging a VM:

```bash
docker compose --env-file generated/infra/.env ps
```

## Compose validation (CI-style)

From repo root after generating a merged env:

```bash
./scripts/install.sh --preset all --smoke-test --yes
docker compose --env-file .smoke-test/.env config --quiet
```

## Caddy / TLS

```bash
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## VPN (gluetun)

```bash
docker compose logs gluetun --tail 50
docker compose exec gluetun wget -qO- https://ipinfo.io/ip
```

## Ansible

```bash
ansible-playbook -i automation/ansible/inventory/hosts.yml automation/ansible/site.yml --syntax-check
ansible-playbook ... -vvv
```

## Config / IaC out of sync

```bash
homelab-toolkit --root . generate
```

Then re-run `tofu plan` and redeploy guests if IPs or sizing changed.

## Getting help

- [Homelab UI](homelab-ui.md) — watchdog, maintenance, deploy progress
- [Configuration](configuration.md) — `config.yaml` and generated files
- [Automation](automation.md) — Ansible order and commands
- [Homelab UI](homelab-ui.md) — web dashboard and wizard
