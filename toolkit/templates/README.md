# Framework Templates

This directory contains framework-owned presentation templates, currently the
invitation email layouts. Runtime service configuration is not shared here.

Each service keeps its generated configuration templates under
`toolkit/services/<name>/templates/` and renders them from its declared
`generate_artifacts()` hook. See `docs/adding-a-new-service.md`.
