from __future__ import annotations

from types import SimpleNamespace

from toolkit.core.config.config import Config
from toolkit.core.manifest.artifacts import compile_config_sources, compile_generated_artifacts
from toolkit.core.manifest.schema import ServiceManifest


def test_runtime_scoped_artifact_expands_to_every_runtime_node() -> None:
    manifest = ServiceManifest(
        name="example",
        label="Example",
        description="Example runtime",
        category="management",
        placement="infra",
        icon="box",
        priority=1,
        enabled_when=[{"path": "network.expose_via_internet", "equals": True}],
        runtimes={
            "example-agent": {
                "placements": ["@non-primary"],
                "compose_profile": "agents",
            }
        },
        generated_artifacts=[
            {
                "path": "generated/example-agent.conf",
                "runtime_service": "example-agent",
                "sensitive": True,
            }
        ],
    )
    catalog = SimpleNamespace(manifests=(manifest,))

    artifacts = compile_generated_artifacts(Config(network={"expose_via_internet": True}), catalog)

    assert [(artifact.path, artifact.node) for artifact in artifacts] == [
        ("example-agent.conf", "apps"),
        ("example-agent.conf", "media"),
    ]

    disabled = compile_generated_artifacts(Config(network={"expose_via_internet": False}), catalog)
    assert [(artifact.node, artifact.enabled) for artifact in disabled] == [
        ("apps", False),
        ("media", False),
    ]


def test_explicit_artifact_mode_overrides_sensitive_default() -> None:
    manifest = ServiceManifest(
        name="example",
        label="Example",
        description="Rootless runtime config",
        category="management",
        placement="infra",
        icon="box",
        priority=1,
        generated_artifacts=[{"path": "generated/example.json", "sensitive": True, "host_uid": 1000, "host_gid": 1000}],
    )
    catalog = SimpleNamespace(manifests=(manifest,))

    artifacts = compile_generated_artifacts(Config(), catalog)

    assert artifacts[0].mode == "0600"
    assert (artifacts[0].host_uid, artifacts[0].host_gid) == (1000, 1000)


def test_config_source_compiler_retains_inactive_variant_for_cleanup() -> None:
    manifest = ServiceManifest(
        name="example",
        label="Example",
        description="Example runtime",
        category="management",
        placement="infra",
        icon="box",
        priority=1,
        host_sources={
            "EXAMPLE_CONFIG_SOURCE": {
                "path": "config/example-private",
                "static": True,
                "variants": [
                    {
                        "when": {"path": "network.expose_via_internet", "equals": True},
                        "path": "config/example-public",
                    }
                ],
            }
        },
    )
    catalog = SimpleNamespace(manifests=(manifest,))
    cfg = Config(network={"expose_via_internet": False})

    sources = compile_config_sources(cfg, catalog)

    assert [(source.path, source.enabled) for source in sources] == [
        ("config/example-private", True),
        ("config/example-public", False),
    ]
