from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from toolkit.core.config.storage import secrets_path, sops_config_path

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import RequiredSecretManifest


class SecretTier(StrEnum):
    USER = "user"  # User must provide (Plex claim, Cloudflare token, etc.)
    GENERATED = "gen"  # Auto-generated passwords/keys
    DERIVED = "derived"  # Computed from other values (postgres URLs, etc.)


class RotationPolicy(StrEnum):
    """How a generated secret may be changed safely."""

    RESTART = "restart"
    RECONCILE = "reconcile"
    PERSISTENT = "persistent"


class SecretSpec:
    def __init__(
        self,
        name: str,
        tier: SecretTier,
        length: int = 32,
        description: str = "",
        *,
        hex_only: bool = False,
        password: bool = False,
        default: str | None = None,
        rotation: RotationPolicy = RotationPolicy.PERSISTENT,
    ):
        self.name = name
        self.tier = tier
        self.length = length
        self.description = description
        self.hex_only = hex_only
        self.password = password
        self.default = default
        self.rotation = rotation


# Fixed infrastructure credentials that are not owned by a runtime service.
INFRASTRUCTURE_SECRETS: list[SecretSpec] = [
    SecretSpec(
        "PROXMOX_API_TOKEN_ID",  # gitleaks:allow - manifest key name, never a credential value
        SecretTier.USER,
        description="Proxmox API token ID",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "PROXMOX_API_TOKEN_SECRET",
        SecretTier.USER,
        description="Proxmox API token secret",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "HOMELAB_SSH_PRIVATE_KEY",
        SecretTier.GENERATED,
        0,
        "SSH private key for Proxmox and managed guests",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "HOMELAB_SSH_PUBLIC_KEY",
        SecretTier.GENERATED,
        0,
        "Matching automation SSH public key",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "PROXMOX_HOST_SSH_KEY",
        SecretTier.USER,
        0,
        "Existing Proxmox host SSH private key",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "AGE_KEY_BACKUP_ATTEST",
        SecretTier.USER,
        description="Attestation that the age identity is backed up off-controller",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "CLOUDFLARE_API_TOKEN",
        SecretTier.USER,
        description="Cloudflare DNS API token",
        rotation=RotationPolicy.PERSISTENT,
    ),
    SecretSpec(
        "CLOUDFLARE_ZONE_ID", SecretTier.USER, description="Cloudflare DNS zone ID", rotation=RotationPolicy.PERSISTENT
    ),
]

DEPLOY_NOTIFICATION_SECRET = SecretSpec(
    "DEPLOY_NTFY_URL",
    SecretTier.USER,
    description="ntfy topic URL for deployment notifications",
    rotation=RotationPolicy.PERSISTENT,
)


def generate_secret(length: int = 32) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_password(length: int = 32) -> str:
    """Generate a shell- and Compose-safe password with common complexity classes."""
    if length < 4:
        raise ValueError("password length must be at least 4")
    classes = (
        "abcdefghjkmnpqrstuvwxyz",
        "ABCDEFGHJKMNPQRSTUVWXYZ",
        "23456789",
        ".*+?-",
    )
    characters = [secrets.choice(group) for group in classes]
    alphabet = "".join(classes)
    characters.extend(secrets.choice(alphabet) for _ in range(length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def generated_password_is_valid(value: str) -> bool:
    """Return whether a stored value still satisfies the password generator contract."""
    alphabet = set("abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789.*+?-")
    return (
        len(value) >= 4
        and set(value) <= alphabet
        and any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
        and any(character in ".*+?-" for character in value)
    )


def generate_ssh_key_pair(root: Path | None = None) -> tuple[str, str]:
    """Generate an ed25519 SSH key pair. Returns (private_key, public_key).

    If root is provided, also writes the key files to ssh/homelab_admin_ed25519
    and ssh/homelab_admin_ed25519.pub under the repo root.
    """
    key_dir = root / "ssh" if root else None
    if key_dir:
        key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    private_key_path = key_dir / "homelab_admin_ed25519" if key_dir else None
    public_key_path = key_dir / "homelab_admin_ed25519.pub" if key_dir else None

    # Check if keys already exist on disk
    if private_key_path and private_key_path.is_file() and public_key_path and public_key_path.is_file():
        return private_key_path.read_text().strip(), public_key_path.read_text().strip()

    try:
        _ = subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(private_key_path) if private_key_path else "/dev/null",
                "-N",
                "",
                "-C",
                "homelab-admin",
                "-q",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        if private_key_path:
            private_key_path.chmod(0o600)
        if public_key_path and public_key_path.is_file():
            pub = public_key_path.read_text().strip()
            priv = private_key_path.read_text().strip() if private_key_path else ""
            return priv, pub
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: generate in-memory using cryptography library
    try:
        from cryptography.hazmat.primitives import serialization as crypto_serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key = ed25519.Ed25519PrivateKey.generate()
        private_bytes = key.private_bytes(
            encoding=crypto_serialization.Encoding.PEM,
            format=crypto_serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=crypto_serialization.NoEncryption(),
        )
        public_bytes = key.public_key().public_bytes(
            encoding=crypto_serialization.Encoding.OpenSSH,
            format=crypto_serialization.PublicFormat.OpenSSH,
        )
        priv = private_bytes.decode()
        pub = "ssh-ed25519 " + public_bytes.decode().split(" ")[1] + " homelab-admin"

        if private_key_path:
            private_key_path.write_text(priv)
            private_key_path.chmod(0o600)
        if public_key_path:
            public_key_path.write_text(pub + "\n")
        return priv, pub
    except ImportError:
        raise RuntimeError(
            "ssh-keygen not found and cryptography library not installed. "
            "Install ssh-keygen or restore the locked environment with: uv sync --locked"
        )


def ensure_ssh_keys(root: Path) -> tuple[str, str]:
    """Ensure SSH key pair exists, generating if needed. Returns (priv, pub)."""
    return generate_ssh_key_pair(root)


def manifest_secret_spec(entry: RequiredSecretManifest) -> SecretSpec:
    tier = {
        "user": SecretTier.USER,
        "generated": SecretTier.GENERATED,
        "bootstrapped": SecretTier.DERIVED,
        "derived": SecretTier.DERIVED,
    }[entry.tier]
    return SecretSpec(
        name=entry.name,
        tier=tier,
        length=entry.length,
        description=entry.description,
        password=entry.generator == "password",
        default=entry.default,
        rotation=RotationPolicy(entry.rotation),
    )


def get_required_secrets(config: Config, catalog: ServiceCatalog | None = None) -> list[SecretSpec]:
    """Return fixed infrastructure and enabled manifest-owned secret requirements."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.core.projects.secrets import project_database_secret_name

    specs = list(INFRASTRUCTURE_SECRETS)
    if config.images.auth.token_secret:
        specs.append(
            SecretSpec(
                config.images.auth.token_secret,
                SecretTier.USER,
                description=f"Read-only container registry token for {config.images.registry}",
                rotation=RotationPolicy.PERSISTENT,
            )
        )
    if config.notifications.smtp.mode == "external" and config.notifications.smtp.password_secret:
        specs.append(
            SecretSpec(
                config.notifications.smtp.password_secret,
                SecretTier.USER,
                description=f"SMTP password for {config.notifications.smtp.username}",
                rotation=RotationPolicy.PERSISTENT,
            )
        )
    if config.category_enabled("notifications"):
        specs.append(DEPLOY_NOTIFICATION_SECRET)
    catalog = catalog or load_service_catalog()
    for manifest in catalog.manifests:
        if service_is_enabled(config, manifest, catalog):
            specs.extend(manifest_secret_spec(entry) for entry in manifest.required_secrets)
    specs.extend(
        SecretSpec(
            project_database_secret_name(project.subdomain),
            SecretTier.GENERATED,
            32,
            f"PostgreSQL password for managed project {project.subdomain}",
            rotation=RotationPolicy.RECONCILE,
        )
        for project in sorted(config.projects.entries, key=lambda item: item.subdomain)
        if project.database_service
    )

    by_name: dict[str, SecretSpec] = {}
    for spec in specs:
        existing = by_name.get(spec.name)
        if existing is not None and (
            existing.tier != spec.tier
            or existing.length != spec.length
            or existing.description != spec.description
            or existing.hex_only != spec.hex_only
            or existing.password != spec.password
            or existing.default != spec.default
            or existing.rotation != spec.rotation
        ):
            raise ValueError(f"conflicting secret ownership for {spec.name}")
        by_name[spec.name] = spec
    return list(by_name.values())


def generate_all_secrets(
    specs: list[SecretSpec],
    existing: dict[str, str] | None = None,
    *,
    root: Path | None = None,
) -> dict[str, str]:
    existing = existing or {}
    result = dict(existing)
    ssh_priv = ""
    ssh_pub = ""

    # Pre-generate SSH key pair if root is provided and keys are needed
    needs_ssh = any(
        s.name in ("HOMELAB_SSH_PRIVATE_KEY", "HOMELAB_SSH_PUBLIC_KEY") and not result.get(s.name) for s in specs
    )
    if needs_ssh and root:
        try:
            ssh_priv, ssh_pub = ensure_ssh_keys(root)
        except (RuntimeError, OSError) as exc:
            import logging

            logging.getLogger(__name__).warning("SSH key generation failed: %s", exc)

    for spec in specs:
        if spec.name in result and result[spec.name]:
            if spec.password and not generated_password_is_valid(result[spec.name]):
                if spec.rotation == RotationPolicy.PERSISTENT:
                    raise ValueError(
                        f"persistent generated password {spec.name} violates its declared generator contract"
                    )
                result[spec.name] = generate_password(spec.length)
            continue
        if spec.tier == SecretTier.GENERATED:
            if spec.name == "HOMELAB_SSH_PRIVATE_KEY":
                result[spec.name] = ssh_priv
                continue
            if spec.name == "HOMELAB_SSH_PUBLIC_KEY":
                result[spec.name] = ssh_pub
                continue
            if spec.default is not None:
                result.setdefault(spec.name, spec.default)
                continue
            if spec.hex_only:
                result[spec.name] = secrets.token_hex(spec.length // 2)
            elif spec.password:
                result[spec.name] = generate_password(spec.length)
            else:
                result[spec.name] = generate_secret(spec.length)
        elif spec.tier == SecretTier.USER and spec.name not in result:
            result[spec.name] = ""
    return result


def extract_lxc_root_passwords(root: Path) -> dict[str, str]:
    """Extract LXC root passwords from terraform state output.

    Returns a dict like {'infra': 'password123', 'media': 'password456', ...}
    or empty dict if terraform state is unavailable.
    """
    import json
    import subprocess as _subprocess

    infra_dir = root / "infrastructure"
    if not (infra_dir / "terraform.tfstate").exists():
        return {}

    try:
        result = _subprocess.run(
            ["tofu", "output", "-json", "lxc_root_passwords"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(infra_dir),
            check=False,
        )
        if result.returncode != 0:
            # Fall back to terraform
            result = _subprocess.run(
                ["terraform", "output", "-json", "lxc_root_passwords"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(infra_dir),
                check=False,
            )
        if result.returncode == 0 and result.stdout.strip():
            passwords = json.loads(result.stdout)
            if isinstance(passwords, dict):
                return {k: v for k, v in passwords.items() if v}
    except (OSError, json.JSONDecodeError, _subprocess.TimeoutExpired):
        pass
    return {}


def merge_secret_values(root: Path, updates: dict[str, str]) -> list[str]:
    """Merge non-empty secret updates into the install secrets file."""
    logs: list[str] = []
    filtered = {k: v for k, v in updates.items() if v}
    if not filtered:
        return logs

    path = secrets_path(root)
    guest_role = os.environ.get("HOMELAB_NODE", "").strip()
    if guest_role and not path.is_file():
        names = ", ".join(sorted(filtered))
        return [
            f"Secrets: runtime values discovered on scoped {guest_role} guest ({names}); controller store unchanged"
        ]
    current = load_secrets_plaintext(path)
    changed = False
    for key, value in filtered.items():
        if current.get(key) == value:
            continue
        current[key] = value
        changed = True
        logs.append(f"Secrets: saved {key}")

    if changed:
        save_secrets_plaintext(current, path)
    return logs


def secrets_encryption_available() -> bool:
    """Return whether SOPS and age-keygen are available locally."""
    return bool(shutil.which("sops") and shutil.which("age-keygen"))


class SecretStoreUnavailableError(RuntimeError):
    """Raised when encrypted secret storage cannot be used safely."""


def _test_plaintext_secrets_allowed() -> bool:
    """Allow plaintext only for explicitly opted-in pytest fixtures."""
    return os.environ.get("HOMELAB_TEST_PLAINTEXT_SECRETS") == "1" and bool(os.environ.get("PYTEST_CURRENT_TEST"))


def secrets_should_be_encrypted(path: Path) -> bool:
    """Return whether this path is intended to store encrypted secrets."""
    return path.name.endswith(".enc.yaml")


def secrets_file_is_encrypted(path: Path) -> bool:
    """Best-effort check whether a file contains SOPS-encrypted content."""
    if not path.exists():
        return False
    raw = path.read_text()
    # SOPS may output JSON with tabs; check for sops metadata line first
    if '"sops"' in raw or "'sops'" in raw:
        return True
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return False
    return isinstance(parsed, dict) and "sops" in parsed


def secret_storage_mode(path: Path) -> str:
    """Return encrypted/plaintext/missing for the current secrets file."""
    if not path.exists():
        return "missing"
    if secrets_file_is_encrypted(path):
        return "encrypted"
    return "plaintext"


def _age_public_key_from_file(age_key_path: Path) -> str:
    try:
        if not age_key_path.is_file():
            return ""
        for line in age_key_path.read_text().splitlines():
            if line.startswith("# public key:"):
                return line.split(": ", 1)[1].strip()
    except OSError:
        return ""
    return ""


def ensure_sops_ready(root: Path) -> str:
    """Ensure a usable .sops.yaml and age key exist for this root.
    Returns public key if SOPS is available, empty string if age-keygen is missing."""
    sops_cfg = sops_config_path(root)
    if sops_cfg.exists():
        for key_file in _sops_age_key_candidates(root):
            pub = _age_public_key_from_file(key_file)
            if pub:
                return pub
        try:
            parsed = yaml.safe_load(sops_cfg.read_text()) or {}
            for rule in parsed.get("creation_rules", []):
                age = str(rule.get("age", "")).strip()
                if age:
                    return age
        except yaml.YAMLError:
            pass
    try:
        return init_sops(root)
    except (FileNotFoundError, RuntimeError):
        return ""


def load_secrets_plaintext(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if secrets_file_is_encrypted(path):
        decrypted = sops_decrypt(path)
        if decrypted is None:
            raise RuntimeError(
                f"Failed to decrypt secrets file {path}. Check that SOPS and age keys are properly configured."
            )
        return decrypted
    raw = yaml.safe_load(path.read_text()) or {}
    return {k: str(v) for k, v in raw.items() if v is not None}


def load_runtime_secrets(root: Path, *, role: str | None = None) -> dict[str, str]:
    """Load authoritative controller secrets or a guest's role-scoped bundle."""
    role_name = (role or os.environ.get("HOMELAB_NODE", "")).strip()
    role_values: dict[str, str] = {}
    if role_name:
        from dotenv import dotenv_values

        from toolkit.core.config.storage import hook_bundle_path, hook_env_path

        candidates = [hook_env_path(role_name, root)]
        controller_local = os.environ.get("HOMELAB_CONTROLLER_ROLE", "").strip().lower() == "local"
        if not os.environ.get("HOMELAB_NODE") or controller_local:
            candidates.append(hook_bundle_path(role_name, root))
        for candidate in candidates:
            if candidate.is_file():
                values = dotenv_values(candidate)
                role_values = {key: value for key, value in values.items() if value is not None}
                break

    if role_name:
        return role_values

    path = secrets_path(root)
    if path.exists():
        return load_secrets_plaintext(path)
    return {}


def save_secrets_plaintext(secrets_dict: dict[str, str], path: Path) -> None:
    # Temp file lives next to the target so the final replace is an atomic
    # same-filesystem rename (mkstemp in /tmp breaks on tmpfs) and so SOPS
    # resolves the repo's .sops.yaml regardless of process CWD.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_path = tempfile.mkstemp(prefix="secrets.enc.tmp.", suffix=".yaml", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        tmp.write_text(yaml.dump(secrets_dict, default_flow_style=False, sort_keys=True))
        tmp.chmod(0o600)
        if secrets_should_be_encrypted(path):
            if not secrets_encryption_available():
                if not _test_plaintext_secrets_allowed():
                    raise SecretStoreUnavailableError(
                        f"Refusing plaintext write for encrypted secrets file {path}: SOPS and age-keygen are required"
                    )
            else:
                recipient = ensure_sops_ready(path.parent)
                if not recipient or not sops_encrypt(tmp, root=path.parent, age_recipient=recipient):
                    raise SecretStoreUnavailableError(f"Failed to encrypt secrets file {path}")
                tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def rotate_secrets(
    root: Path,
    specific: list[str] | None = None,
) -> dict[str, str]:
    """Regenerate specified secrets. Returns dict of rotated secret names to new values."""
    current = load_secrets_plaintext(secrets_path(root))
    rotated = {}

    from toolkit.core.config.config import config_path, load_config

    cfg = load_config(config_path(root))
    specs = get_required_secrets(cfg)
    if specific:
        requested = set(specific)
        known = {spec.name for spec in specs}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(f"unknown secret(s): {', '.join(unknown)}")
        persistent = sorted(
            spec.name for spec in specs if spec.name in requested and spec.rotation == RotationPolicy.PERSISTENT
        )
        if persistent:
            raise ValueError(
                "secret(s) require a service-owned migration and cannot be rotated automatically: "
                + ", ".join(persistent)
            )
    for spec in specs:
        if specific and spec.name not in specific:
            continue
        if spec.tier != SecretTier.GENERATED:
            continue
        if spec.rotation == RotationPolicy.PERSISTENT:
            continue
        # CRITICAL: SSH key specs have length==0 and are generated specially
        # (not via generate_secret) — skipping them here prevents blanking the
        # real key with generate_secret(0) == "". Hex_only secrets must use
        # token_hex, not the alphanumeric alphabet.
        if spec.length == 0:
            continue  # SSH keys + zero-length specs are never rotated here.
        if spec.hex_only:
            new_value = secrets.token_hex(spec.length // 2)
        elif spec.password:
            new_value = generate_password(spec.length)
        else:
            new_value = generate_secret(spec.length)
        current[spec.name] = new_value
        rotated[spec.name] = new_value

    save_secrets_plaintext(current, secrets_path(root))
    return rotated


def sops_encrypt(plaintext_path: Path, root: Path | None = None, age_recipient: str = "") -> bool:
    """Encrypt a plaintext YAML file with SOPS+age. Returns True on success.

    Prefers an explicit ``--age`` recipient (robust against creation-rule
    """
    sops_cfg = sops_config_path(root)
    if not age_recipient and not sops_cfg.exists():
        return False
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", dir=plaintext_path.parent)
    os.close(fd)
    tmp = Path(tmp_path)
    cmd = ["sops"]
    if age_recipient:
        cmd += ["--age", age_recipient]
    else:
        cmd += ["--config", str(sops_cfg)]
    cmd += [
        "--encrypt",
        "--input-type",
        "yaml",
        "--output-type",
        "yaml",
        "--output",
        str(tmp),
        str(plaintext_path),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=30,
            env=_sops_env(plaintext_path.parent),
        )
        tmp.replace(plaintext_path)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    finally:
        tmp.unlink(missing_ok=True)


def init_sops(root: Path) -> str:
    """Initialize SOPS encryption: generate age key + .sops.yaml. Returns public key."""
    age_key_path = root / "keys" / "age.key"
    age_key_path.parent.mkdir(parents=True, exist_ok=True)

    if age_key_path.exists():
        for line in age_key_path.read_text().splitlines():
            if line.startswith("# public key:"):
                return line.split(": ", 1)[1].strip()
        raise ValueError("age key exists but no public key found")

    result = subprocess.run(
        ["age-keygen", "-o", str(age_key_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"age-keygen failed: {result.stderr}")

    public_key = ""
    for line in result.stderr.splitlines():
        if line.startswith("Public key:"):
            public_key = line.split(": ", 1)[1].strip()
            break

    if not public_key:
        for line in age_key_path.read_text().splitlines():
            if line.startswith("# public key:"):
                public_key = line.split(": ", 1)[1].strip()
                break

    if not public_key:
        raise RuntimeError("Could not extract public key from age-keygen output")

    sops_config = root / ".sops.yaml"
    sops_config.write_text(f"""creation_rules:
  - path_regex: secrets\\.enc\\.yaml$
    age: >-
      {public_key}
""")

    age_key_path.chmod(0o600)
    return public_key


def _sops_age_key_candidates(near: Path) -> list[Path]:
    """Root-owned key first, then explicit and guest-level key locations."""
    env_key = os.environ.get("SOPS_AGE_KEY_FILE", "").strip()
    candidates: list[Path] = [near / "keys" / "age.key"]
    if env_key:
        candidates.append(Path(env_key))
    candidates.extend(
        [
            Path("/root/homelab-age.key"),
            Path("/root/.config/sops/age/keys.txt"),
            Path.home() / ".config" / "sops" / "age" / "keys.txt",
        ]
    )
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _sops_env(near: Path) -> dict[str, str]:
    """Process env for sops with the install root's key taking precedence."""
    env = dict(os.environ)
    for key_file in _sops_age_key_candidates(near):
        try:
            if key_file.is_file():
                env["SOPS_AGE_KEY_FILE"] = str(key_file)
                break
        except OSError:
            continue
    return env


def sops_decrypt(encrypted_path: Path) -> dict[str, str] | None:
    """Decrypt a SOPS-encrypted YAML file. Returns dict of key-value pairs, or None on failure."""
    if not encrypted_path.exists():
        return None
    try:
        result = subprocess.run(
            ["sops", "--decrypt", str(encrypted_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=_sops_env(encrypted_path.parent),
        )
        raw = yaml.safe_load(result.stdout) or {}
        return {k: str(v) for k, v in raw.items() if v is not None}
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, yaml.YAMLError):
        return None
