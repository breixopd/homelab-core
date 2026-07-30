"""Bounded writer for service-owned generated runtime artifacts."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from toolkit.core.config.config import Config
from toolkit.core.manifest.schema import GeneratedArtifactManifest, ServiceManifest

if TYPE_CHECKING:
    from toolkit.services import ServicePlugin


class GeneratedArtifactError(RuntimeError):
    """A service generator violated its declared artifact contract."""


def _env_escape(value: object) -> str:
    text = str(value)
    if not text:
        return ""
    if any(character in text for character in ' #$"\\`\n\t'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("$", "$$").replace("`", "\\`").replace("\n", "\\n")
        return f'"{escaped}"'
    return text


def render_env_value(value: object) -> str:
    """Render an arbitrary value as one bounded Docker Compose env-file value."""
    return _env_escape(value)


def _yaml_value(value: object) -> str:
    import yaml

    return yaml.safe_dump(value, default_flow_style=True).strip()


def _json_value(value: object) -> str:
    import json

    return json.dumps(value)


def _caddy_escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


@lru_cache(maxsize=64)
def _template_environment(service: str) -> Environment:
    template_dir = Path(__file__).resolve().parents[2] / "services" / service / "templates"
    env = Environment(  # nosec B701 - renders trusted configuration templates, never HTML input
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=select_autoescape([]),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        env_escape=_env_escape,
        yaml_value=_yaml_value,
        json_value=_json_value,
        caddy_escape=_caddy_escape,
    )
    return env


class ArtifactGenerationContext:
    """Write and verify only the artifacts declared by one service."""

    def __init__(
        self,
        config: Config,
        root: Path,
        secrets: Mapping[str, str],
        manifest: ServiceManifest,
    ) -> None:
        self.config = config
        self.root = root.resolve()
        self.secrets = MappingProxyType(dict(secrets))
        self.manifest = manifest
        self._contracts = {artifact.path: artifact for artifact in manifest.generated_artifacts}
        self._claimed: set[str] = set()

    def _contract(self, relative: str) -> GeneratedArtifactManifest:
        try:
            return self._contracts[relative]
        except KeyError as exc:
            raise GeneratedArtifactError(
                f"service {self.manifest.name!r} attempted undeclared artifact {relative!r}"
            ) from exc

    def artifact_path(self, relative: str) -> Path:
        self._contract(relative)
        path = self.root / relative
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise GeneratedArtifactError(f"cannot resolve generated artifact {relative!r}") from exc
        if not resolved.is_relative_to(self.root):
            raise GeneratedArtifactError(f"generated artifact {relative!r} escapes the repository root")
        return path

    @staticmethod
    def _file_mode(contract: GeneratedArtifactManifest) -> int:
        if contract.mode is not None:
            return int(contract.mode, 8)
        if contract.sensitive:
            return 0o600
        if contract.executable:
            return 0o500
        return 0o644

    def _file_owner(self, relative: str) -> tuple[int, int] | None:
        for asset in self.manifest.data_specs:
            if asset.source_env is None:
                continue
            source = self.manifest.host_sources.get(asset.source_env)
            if source is None:
                continue
            prefix = source.path.rstrip("/") + "/"
            if relative == source.path or relative.startswith(prefix):
                return asset.host_uid, asset.host_gid
        return None

    @staticmethod
    def _artifact_owner(contract: GeneratedArtifactManifest) -> tuple[int, int] | None:
        if contract.host_uid is not None and contract.host_gid is not None:
            return contract.host_uid, contract.host_gid
        return None

    def _apply_file_metadata(self, path: Path, relative: str, contract: GeneratedArtifactManifest) -> None:
        path.chmod(self._file_mode(contract))
        owner = self._artifact_owner(contract) or self._file_owner(relative)
        if owner is not None and os.geteuid() == 0:
            os.chown(path, *owner)

    def write_text(self, relative: str, content: str) -> Path:
        contract = self._contract(relative)
        if contract.kind != "file":
            raise GeneratedArtifactError(f"generated artifact {relative!r} is not a file")
        path = self.artifact_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == content:
            self._apply_file_metadata(path, relative, contract)
            self._claimed.add(relative)
            return path
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._apply_file_metadata(temporary, relative, contract)
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        self._claimed.add(relative)
        return path

    def render_template(self, relative: str, template: str, values: Mapping[str, object]) -> Path:
        content = _template_environment(self.manifest.name).get_template(template).render(**values)
        return self.write_text(relative, content)

    def write_symlink(self, relative: str, target: str) -> Path:
        contract = self._contract(relative)
        if contract.kind != "symlink":
            raise GeneratedArtifactError(f"generated artifact {relative!r} is not a symlink")
        link = self.artifact_path(relative)
        target_path = (self.root / target).resolve(strict=False)
        if not target_path.is_relative_to(self.root):
            raise GeneratedArtifactError(f"generated artifact symlink target {target!r} escapes the repository root")
        link.parent.mkdir(parents=True, exist_ok=True)
        relative_target = os.path.relpath(target_path, link.parent.resolve())
        temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(relative_target)
        os.replace(temporary, link)
        self._claimed.add(relative)
        return link

    def claim(self, relative: str) -> Path:
        contract = self._contract(relative)
        path = self.artifact_path(relative)
        if contract.kind == "symlink":
            valid = path.is_symlink()
        else:
            valid = path.is_file() and not path.is_symlink()
        if not valid:
            raise GeneratedArtifactError(f"declared generated artifact {relative!r} does not exist with the right type")
        if contract.kind == "file":
            self._apply_file_metadata(path, relative, contract)
        self._claimed.add(relative)
        return path

    def finish(self) -> tuple[Path, ...]:
        missing = sorted(set(self._contracts) - self._claimed)
        if missing:
            raise GeneratedArtifactError(
                f"service {self.manifest.name!r} did not produce declared artifacts: {', '.join(missing)}"
            )
        return tuple(self.artifact_path(artifact.path) for artifact in self.manifest.generated_artifacts)


def generate_service_artifacts(
    config: Config,
    root: Path,
    secrets: Mapping[str, str],
    *,
    plugins: Iterable[ServicePlugin] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
    """Generate enabled service artifacts in deterministic manifest order."""
    if plugins is None:
        from toolkit.services import enabled_service_plugins

        selected = [plugin for _category, plugin in enabled_service_plugins(config)]
    else:
        selected = list(plugins)
    selected = [plugin for plugin in selected if plugin.manifest.generated_artifacts]
    selected.sort(key=lambda plugin: (plugin.manifest.priority, plugin.service))
    written: list[Path] = []
    for completed, plugin in enumerate(selected, start=1):
        if on_progress is not None:
            on_progress(completed, len(selected), plugin.service)
        context = ArtifactGenerationContext(config, root, secrets, plugin.manifest)
        plugin.generate_artifacts(context)
        written.extend(context.finish())
    return written
