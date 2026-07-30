# Service-owned tests

This tree is optional and intentionally outside the framework test gate.
Each directory follows the matching service manifest under `toolkit/services/`.

Run one service's tests explicitly when changing its plugin implementation:

```bash
uv run --locked pytest tests/services/<service>/ -q --timeout=60
```

The framework does not treat these tests as a second source of runtime truth.
Deployment verification is declared by the service manifest and implemented by
its `post_start`, `verify`, and `heal` hooks. New services should add only
focused implementation tests here when they provide value beyond those live
checks. Cross-service contract tests belong under `tests/framework/` and must
remain service-name agnostic wherever possible. Concrete multi-service
orchestration tests belong under `_cross/<area>/` instead.

Ownership conventions:

- `service-catalog/` covers discovery, metadata, resource defaults, and essential-service policy.
- `<service>/` covers that plugin's hooks, runtime integrations, and service-specific controller checks.
- `_cross/<area>/` covers behavior that coordinates multiple service plugins, such as management projections or monitoring.
