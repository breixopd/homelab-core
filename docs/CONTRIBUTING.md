# Contributing

Before opening a pull request, run the verification checks from the repository root:

```bash
# Install the exact locked development environment
uv sync --locked --all-extras

# Lint and format check
uv run --locked ruff check toolkit/ tests/
uv run --locked ruff format --check toolkit/ tests/

# Unit tests
uv run --locked pytest tests/framework/ -q --timeout=60

# Optional service-owned tests (never part of the framework gate)
uv run --locked pytest tests/services/<service>/ -q --timeout=60
uv run --locked pytest tests/services/_cross/<area>/ -q --timeout=60

# Type check
uv run --locked mypy --ignore-missing-imports toolkit/core toolkit/cli toolkit/webui toolkit/controller

# Validate manifest-generated deployment artifacts and Ansible sources
uv run --locked homelab-toolkit --root . generate
ANSIBLE_CONFIG=automation/ansible/ansible.cfg \
  uv run --locked ansible-lint --project-dir automation/ansible automation/ansible
```

`make ci` runs the low-resource local gate. GitHub Actions runs the full Python
matrix, coverage, security, installer, infrastructure, and image checks on push
and pull requests. See `.github/workflows/ci.yml`.
