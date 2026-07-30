"""LLDAP GraphQL client — single source of truth for homelab user accounts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from toolkit.core.identity.service_groups import HOMELAB_GROUP_NAMES
from toolkit.core.ops.automation import docker_exec, resolve_docker_service_url


def resolve_lldap_api_url(root: Path | None = None) -> str:
    """Reachable LLDAP GraphQL base URL (infra LAN IP when multi-VM)."""
    import os

    from toolkit.core.config.ldap import lldap_http_port

    http_port = lldap_http_port()

    if root is not None:
        from toolkit.core.config.config import config_path, load_config

        cfg = load_config(config_path(root))
        if cfg.is_multi_node:
            from toolkit.core.manifest.placement import service_address, service_node

            if os.environ.get("HOMELAB_NODE") == service_node(cfg, "lldap"):
                if os.environ.get("HOMELAB_CONTROLLER_ROLE"):
                    return f"http://lldap:{http_port}"
                local = resolve_docker_service_url("lldap", http_port).rstrip("/")
                if local:
                    return local
            ip = service_address(cfg, "lldap")
            if ip:
                return f"http://{ip}:{http_port}"
    return resolve_docker_service_url("lldap", http_port).rstrip("/")


@dataclass
class LLDAPUser:
    id: str
    email: str
    display_name: str = ""


def user_id_from_email(email: str) -> str:
    normalized = email.strip().lower()
    if normalized.count("@") != 1:
        raise ValueError("email cannot produce a safe LLDAP user ID")
    local, domain = normalized.split("@", 1)
    slug = re.sub(r"[^a-z0-9._-]+", "-", local).strip("-._")
    if not domain or not slug:
        raise ValueError("email cannot produce a safe LLDAP user ID")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:23]}-{digest}"


POSIX_SYSTEM_UID = 2999
POSIX_BASE_UID = 3000
POSIX_USERS_GROUP = "homelab-users"
POSIX_USERS_GID = 3000

HOMELAB_GROUP_GIDS: dict[str, int] = {
    POSIX_USERS_GROUP: POSIX_USERS_GID,
    **{name: POSIX_BASE_UID + index for index, name in enumerate(HOMELAB_GROUP_NAMES, start=1)},
}


class LLDAPClient:
    """Admin API wrapper for LLDAP (used by bootstrap hooks and `homelab-toolkit users`)."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        admin_password: str,
        root: Path | None = None,
        username: str = "admin",
    ) -> None:
        self.base = (base_url or resolve_lldap_api_url(root)).rstrip("/")
        self.admin_password = admin_password
        self.username = username
        self.root = root.resolve() if root is not None else None
        self._token = ""

    @contextmanager
    def _identity_lock(self) -> Iterator[None]:
        if self.root is None:
            from toolkit.core.config.storage import ensure_homelab_state_path

            state_dir = ensure_homelab_state_path()
        else:
            state_dir = self.root / ".homelab-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_dir.chmod(0o700)
        lock_path = state_dir / "lldap-identity.lock"
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def verify_user_password(cls, email: str, password: str, *, root: Path | None = None) -> tuple[bool, str]:
        """Authenticate a user against LLDAP via the simple-login endpoint.

        Returns (ok, user_id). Used by homelab-ui to authenticate against the
        directory instead of a local admin password. No admin password needed.
        """
        normalized_email = email.strip().lower()
        user_id = user_id_from_email(normalized_email)
        if root is not None:
            from toolkit.core.config.config import config_path, load_config

            config = load_config(config_path(root))
            if config.owner_username and config.email.strip().lower() == normalized_email:
                user_id = config.owner_username
        base = resolve_lldap_api_url(root).rstrip("/")
        try:
            resp = httpx.post(
                f"{base}/auth/simple/login",
                json={"username": user_id, "password": password},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            return False, f"LLDAP unreachable: {exc}"
        if resp.status_code == 200 and resp.json().get("token"):
            return True, user_id
        return False, "Invalid credentials"

    def login(self) -> None:
        resp = httpx.post(
            f"{self.base}/auth/simple/login",
            json={"username": self.username, "password": self.admin_password},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLDAP admin login failed (HTTP {resp.status_code})")
        token = resp.json().get("token", "")
        if not token:
            raise RuntimeError("LLDAP admin login returned no token")
        self._token = token

    @property
    def headers(self) -> dict[str, str]:
        if not self._token:
            self.login()
        return {"Authorization": f"Bearer {self._token}"}

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        resp = httpx.post(
            f"{self.base}/api/graphql",
            headers=self.headers,
            json={"query": query, "variables": variables or {}},
            timeout=20,
        )
        body = resp.json()
        if resp.status_code != 200 or body.get("errors"):
            detail = (body.get("errors") or [{}])[0].get("message", resp.status_code)
            raise RuntimeError(f"LLDAP GraphQL error: {detail}")
        return body.get("data") or {}

    def _graphql_ignore_exists(self, query: str, variables: dict | None = None) -> bool:
        try:
            self._graphql(query, variables)
            return True
        except RuntimeError as exc:
            msg = str(exc).lower()
            if any(token in msg for token in ("already", "exist", "unique", "constraint")):
                return False
            raise

    def get_user_attribute(self, user_id: str, name: str) -> str | None:
        data = self._graphql(
            "query($id: String!) { user(userId: $id) { attributes { name value } } }",
            {"id": user_id},
        )
        for attr in (data.get("user") or {}).get("attributes") or []:
            if attr.get("name") == name and attr.get("value"):
                return str(attr["value"][0])
        return None

    def _insert_user_attributes(self, user_id: str, attrs: dict[str, str]) -> None:
        insert = [{"name": k, "value": [v]} for k, v in attrs.items()]
        self._graphql(
            "mutation($user: UpdateUserInput!) { updateUser(user: $user) { ok } }",
            {"user": {"id": user_id, "insertAttributes": insert}},
        )

    def _insert_group_attributes(self, group_id: int, attrs: dict[str, str]) -> None:
        insert = [{"name": k, "value": [v]} for k, v in attrs.items()]
        self._graphql(
            "mutation($group: UpdateGroupInput!) { updateGroup(group: $group) { ok } }",
            {"group": {"id": group_id, "insertAttributes": insert}},
        )

    def ensure_posix_schema(self) -> list[str]:
        """Create LLDAP custom attributes and object classes required by SSSD/PAM."""
        logs: list[str] = []
        schema = self._graphql(
            """
            {
              schema {
                userSchema { attributes { name } ldapObjectClasses { objectClass } }
                groupSchema { attributes { name } ldapObjectClasses { objectClass } }
              }
            }
            """
        )
        user_schema = (schema.get("schema") or {}).get("userSchema") or {}
        group_schema = (schema.get("schema") or {}).get("groupSchema") or {}
        user_attrs = {a.get("name") for a in user_schema.get("attributes") or []}
        group_attrs = {a.get("name") for a in group_schema.get("attributes") or []}
        user_classes = {c.get("objectClass") for c in user_schema.get("ldapObjectClasses") or []}

        for name, attr_type in [
            ("uidNumber", "INTEGER"),
            ("gidNumber", "INTEGER"),
            ("homeDirectory", "STRING"),
            ("unixShell", "STRING"),
            ("sshPublicKey", "STRING"),
        ]:
            if name in user_attrs:
                continue
            if self._graphql_ignore_exists(
                """
                mutation($name: String!, $t: AttributeType!) {
                  addUserAttribute(
                    name: $name, attributeType: $t, isList: false, isVisible: true, isEditable: true
                  ) { ok }
                }
                """,
                {"name": name, "t": attr_type},
            ):
                logs.append(f"schema: user attribute {name}")

        if "gidNumber" not in group_attrs and self._graphql_ignore_exists(
            """
            mutation {
              addGroupAttribute(
                name: "gidNumber", attributeType: INTEGER, isList: false, isVisible: true, isEditable: true
              ) { ok }
            }
            """
        ):
            logs.append("schema: group attribute gidNumber")

        if "posixAccount" not in user_classes and self._graphql_ignore_exists(
            'mutation { addUserObjectClass(name: "posixAccount") { ok } }'
        ):
            logs.append("schema: user objectClass posixAccount")
        return logs

    def _group_gid(self, display_name: str) -> int | None:
        data = self._graphql(
            "query { groups { id displayName attributes { name value } } }",
        )
        for group in data.get("groups") or []:
            if group.get("displayName") != display_name:
                continue
            for attr in group.get("attributes") or []:
                if attr.get("name") == "gidNumber" and attr.get("value"):
                    return int(attr["value"][0])
            gid = HOMELAB_GROUP_GIDS.get(display_name)
            if gid is not None:
                self._insert_group_attributes(int(group["id"]), {"gidNumber": str(gid)})
                return gid
        return None

    def ensure_homelab_users_group(self) -> list[str]:
        """Primary POSIX group for SSH/LDAP users."""
        logs: list[str] = []
        groups = self.list_groups()
        existing = next((g for g in groups if g.get("displayName") == POSIX_USERS_GROUP), None)
        if existing:
            if self._group_gid(POSIX_USERS_GROUP) is not None:
                logs.append(f"group {POSIX_USERS_GROUP} gid ok")
            return logs
        data = self._graphql(
            """
            mutation($req: CreateGroupInput!) {
              createGroupWithDetails(request: $req) { id displayName }
            }
            """,
            {
                "req": {
                    "displayName": POSIX_USERS_GROUP,
                    "attributes": [{"name": "gidNumber", "value": [str(POSIX_USERS_GID)]}],
                }
            },
        )
        row = data.get("createGroupWithDetails") or {}
        logs.append(f"created group {row.get('displayName', POSIX_USERS_GROUP)} (gid {POSIX_USERS_GID})")
        return logs

    def ensure_homelab_group_gids(self) -> list[str]:
        """Set gidNumber on homelab-* groups used by SSSD supplementary membership."""
        logs: list[str] = []
        data = self._graphql("query { groups { id displayName attributes { name value } } }")
        for group in data.get("groups") or []:
            name = str(group.get("displayName") or "")
            gid = HOMELAB_GROUP_GIDS.get(name)
            if gid is None:
                continue
            has_gid = any(a.get("name") == "gidNumber" and a.get("value") for a in group.get("attributes") or [])
            if has_gid:
                continue
            self._insert_group_attributes(int(group["id"]), {"gidNumber": str(gid)})
            logs.append(f"group {name} gidNumber={gid}")
        return logs

    def next_uid(self) -> int:
        data = self._graphql("query { users { id attributes { name value } } }")
        max_uid = POSIX_BASE_UID - 1
        for user in data.get("users") or []:
            for attr in user.get("attributes") or []:
                if attr.get("name") == "uidNumber" and attr.get("value"):
                    try:
                        max_uid = max(max_uid, int(attr["value"][0]))
                    except ValueError:
                        pass
        return max(max_uid + 1, POSIX_BASE_UID)

    def ensure_user_posix(
        self,
        user_id: str,
        *,
        uid: int | None = None,
        gid: int | None = None,
        home: str | None = None,
    ) -> list[str]:
        """Populate POSIX attributes for SSSD/SSH (idempotent)."""
        if user_id == "admin":
            return []
        with self._identity_lock():
            return self._ensure_user_posix_locked(user_id, uid=uid, gid=gid, home=home)

    def _ensure_user_posix_locked(
        self,
        user_id: str,
        *,
        uid: int | None,
        gid: int | None,
        home: str | None,
    ) -> list[str]:
        logs: list[str] = []
        existing_uid = self.get_user_attribute(user_id, "uidNumber")
        if existing_uid:
            return [f"posix {user_id} uid={existing_uid} (already set)"]

        # POSIX schema (gidNumber/uidNumber/...) must exist before we can write
        # POSIX attributes or create groups that carry a gidNumber. Defensive:
        # ensure_posix_schema is idempotent, so calling it here protects direct
        # callers (CLI `users` command) that don't go through bootstrap_lldap_user.
        logs.extend(self.ensure_posix_schema())
        self.ensure_homelab_users_group()
        self.ensure_homelab_group_gids()

        if user_id == "ldap-bind":
            allocated_uid = uid or POSIX_SYSTEM_UID
            primary_gid = gid or POSIX_SYSTEM_UID
            home_dir = home or "/var/empty"
        else:
            allocated_uid = uid or self.next_uid()
            primary_gid = gid or POSIX_USERS_GID
            home_dir = home or f"/home/{user_id}"
            self.ensure_groups(user_id, [POSIX_USERS_GROUP])

        attrs = {
            "uidNumber": str(allocated_uid),
            "gidNumber": str(primary_gid),
            "homeDirectory": home_dir,
            "unixShell": "/bin/bash",
        }
        self._insert_user_attributes(user_id, attrs)
        logs.append(f"posix {user_id} uid={allocated_uid} gid={primary_gid} home={home_dir}")
        return logs

    def ensure_all_users_posix(self) -> list[str]:
        """Backfill POSIX attributes for every non-admin directory user."""
        logs: list[str] = []
        for user in self.list_users():
            if user.id == "admin":
                continue
            logs.extend(self.ensure_user_posix(user.id))
        return logs

    def list_users(self) -> list[LLDAPUser]:
        data = self._graphql("{ users { id email displayName } }")
        return [
            LLDAPUser(id=str(u["id"]), email=u.get("email") or "", display_name=u.get("displayName") or "")
            for u in data.get("users") or []
        ]

    def list_groups(self) -> list[dict]:
        data = self._graphql("{ groups { id displayName users { id } } }")
        return list(data.get("groups") or [])

    def create_group(self, name: str) -> int | None:
        data = self._graphql(
            "mutation CreateGroup($name: String!) { createGroup(name: $name) { id displayName } }",
            {"name": name},
        )
        row = data.get("createGroup") or {}
        gid = row.get("id")
        return int(gid) if gid is not None else None

    def ensure_homelab_groups(self, group_names: list[str] | None = None) -> list[str]:
        """Create homelab service groups if missing."""
        from toolkit.core.identity.service_groups import HOMELAB_GROUP_NAMES

        wanted = list(group_names or HOMELAB_GROUP_NAMES)
        data = self._graphql("{ groups { id displayName } }")
        existing = {g.get("displayName") for g in data.get("groups") or []}
        logs: list[str] = []
        for gname in wanted:
            if gname in existing:
                continue
            gid = self.create_group(gname)
            if gid is not None:
                logs.append(f"created group {gname}")
                existing.add(gname)
            else:
                logs.append(f"failed to create group {gname}")
        return logs

    def user_group_names(self, user_id: str) -> list[str]:
        data = self._graphql("{ groups { displayName users { id } } }")
        names: list[str] = []
        for group in data.get("groups") or []:
            members = {u.get("id") for u in group.get("users") or []}
            if user_id in members:
                names.append(str(group.get("displayName") or ""))
        return [n for n in names if n]

    def set_user_groups(self, user_id: str, group_names: list[str]) -> list[str]:
        """Replace homelab group membership (preserves non-homelab groups like lldap_admin)."""
        from toolkit.core.identity.service_groups import HOMELAB_GROUP_NAMES

        homelab = set(HOMELAB_GROUP_NAMES)
        data = self._graphql("{ groups { id displayName users { id } } }")
        groups = data.get("groups") or []
        by_name = {g.get("displayName"): g for g in groups}
        logs: list[str] = []
        for gname in group_names:
            if gname not in by_name:
                logs.append(f"group {gname} not found")
        for group in groups:
            gname = str(group.get("displayName") or "")
            gid = group.get("id")
            if gid is None:
                continue
            members = {u.get("id") for u in group.get("users") or []}
            in_group = user_id in members
            should = gname in group_names
            if gname in homelab and in_group and not should:
                self._graphql(
                    "mutation Remove($u: String!, $g: Int!) { removeUserFromGroup(userId: $u, groupId: $g) { ok } }",
                    {"u": user_id, "g": gid},
                )
                logs.append(f"removed {user_id} from {gname}")
            elif should and not in_group:
                self._graphql(
                    "mutation Add($u: String!, $g: Int!) { addUserToGroup(userId: $u, groupId: $g) { ok } }",
                    {"u": user_id, "g": gid},
                )
                logs.append(f"added {user_id} to {gname}")
        return logs

    def create_user(
        self,
        email: str,
        *,
        display_name: str | None = None,
        user_id: str | None = None,
        posix_uid: int | None = None,
        posix_gid: int | None = None,
        posix_home: str | None = None,
    ) -> LLDAPUser:
        user_id = user_id or user_id_from_email(email)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", user_id) or user_id in {"admin", "ldap-bind"}:
            raise ValueError("user ID is invalid or reserved")
        name = display_name or user_id.replace(".", " ").replace("_", " ").title()
        parts = name.split(" ", 1)
        data = self._graphql(
            """
            mutation CreateUser($user: CreateUserInput!) {
              createUser(user: $user) { id email displayName }
            }
            """,
            {
                "user": {
                    "id": user_id,
                    "email": email.strip().lower(),
                    "displayName": name,
                    "firstName": parts[0],
                    "lastName": parts[1] if len(parts) > 1 else parts[0],
                }
            },
        )
        row = data.get("createUser") or {}
        user = LLDAPUser(id=str(row.get("id", user_id)), email=row.get("email") or email, display_name=name)
        try:
            self.ensure_user_posix(user.id, uid=posix_uid, gid=posix_gid, home=posix_home)
        except Exception:
            try:
                self.delete_user(user.id)
            except RuntimeError:
                pass
            raise
        return user

    def set_password(self, user_id: str, password: str) -> None:
        payload = json.dumps(
            {
                "token": self._token or self.headers["Authorization"].removeprefix("Bearer "),
                "username": user_id,
                "password": password,
            },
            separators=(",", ":"),
        )
        rc, out = self._run_password_helper(payload)
        if rc != 0:
            raise RuntimeError(f"LLDAP password update failed: {(out or '')[:200]}")

    def _run_password_helper(self, payload: str) -> tuple[int, str]:
        from toolkit.core.config.ldap import lldap_http_port

        helper = (
            "payload=$(cat); "
            "token=$(printf '%s' \"$payload\" | jq -er .token); "
            "username=$(printf '%s' \"$payload\" | jq -er .username); "
            "LLDAP_USER_PASSWORD=$(printf '%s' \"$payload\" | jq -er .password); "
            "export LLDAP_USER_PASSWORD; "
            f"exec /app/lldap_set_password --base-url http://localhost:{lldap_http_port()} "
            '--token "$token" --username "$username"'
        )
        if self.root is not None:
            from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
            from toolkit.core.config.config import load_config
            from toolkit.core.config.storage import config_path

            cfg = load_config(config_path(self.root))
            if cfg.is_multi_node:
                from toolkit.core.manifest.placement import service_address

                rc, stdout, stderr = ssh_run_on_vm(
                    cfg,
                    service_address(cfg, "lldap"),
                    f"docker exec -i lldap sh -ec {shlex.quote(helper)}",
                    root=self.root,
                    timeout=120,
                    stdin=payload,
                )
                return rc, ((stdout or "") + (stderr or "")).strip()
        return docker_exec(
            "lldap",
            ["sh", "-ec", helper],
            stdin=payload,
        )

    def delete_user(self, user_id: str) -> None:
        if user_id == "admin":
            raise RuntimeError("Refusing to delete built-in admin account")
        self._graphql(
            "mutation DeleteUser($id: String!) { deleteUser(userId: $id) { ok } }",
            {"id": user_id},
        )

    def update_user_email(self, user_id: str, email: str) -> None:
        self._graphql(
            "mutation UpdateUser($user: UpdateUserInput!) { updateUser(user: $user) { ok } }",
            {"user": {"id": user_id, "email": email.strip().lower()}},
        )

    def find_user(self, email: str) -> LLDAPUser | None:
        normalized = email.strip().lower()
        for user in self.list_users():
            if user.email.strip().lower() == normalized:
                return user
        return None

    def ensure_service_bind(self, password: str, *, domain: str = "") -> list[str]:
        """Create or update the ldap-bind service account used by Authelia and SSSD."""
        logs: list[str] = []
        user_id = "ldap-bind"
        if domain and domain != "localhost":
            email = f"ldap-bind@{domain}"
        else:
            email = "ldap-bind@home.local"

        existing = next((u for u in self.list_users() if u.id == user_id), None)
        if existing:
            logs.append(f"service account {user_id} already exists")
        else:
            data = self._graphql(
                """
                mutation CreateUser($user: CreateUserInput!) {
                  createUser(user: $user) { id email displayName }
                }
                """,
                {
                    "user": {
                        "id": user_id,
                        "email": email,
                        "displayName": "LDAP Service Bind",
                        "firstName": "LDAP",
                        "lastName": "Bind",
                    }
                },
            )
            row = data.get("createUser") or {}
            logs.append(f"created service account {row.get('id', user_id)} ({email})")

        self.set_password(user_id, password)
        logs.append(f"password updated for {user_id}")

        # Authelia needs read (SSO search) + password reset (self-service invites).
        for line in self.ensure_groups(user_id, ["lldap_strict_readonly", "lldap_password_manager"]):
            logs.append(line)
        logs.extend(self.ensure_user_posix(user_id))
        return logs

    def ensure_owner(
        self,
        email: str,
        password: str,
        *,
        domain: str = "",
        groups: list[str] | None = None,
        user_id: str | None = None,
    ) -> list[str]:
        """Bootstrap owner account: move admin email if needed, create/update user, set password, groups."""
        logs: list[str] = []
        email = email.strip().lower()
        existing = self.find_user(email)
        desired_user_id = user_id or (
            existing.id if existing is not None and existing.id != "admin" else user_id_from_email(email)
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", desired_user_id) or desired_user_id in {
            "admin",
            "ldap-bind",
        }:
            raise ValueError("owner user ID is invalid or reserved")

        if existing and existing.id != desired_user_id:
            conflicting = next((candidate for candidate in self.list_users() if candidate.id == desired_user_id), None)
            if conflicting is not None:
                raise RuntimeError(f"LLDAP owner username {desired_user_id} already belongs to {conflicting.email}")

        moved_admin_email: str | None = None
        if existing and existing.id == "admin" and existing.email == email:
            if domain and domain != "localhost":
                admin_email = f"lldap-admin@{domain}"
            else:
                admin_email = "lldap-admin@home.local"
            moved_admin_email = existing.email
            self.update_user_email("admin", admin_email)
            logs.append(f"LLDAP: moved admin account email to {admin_email}")
            try:
                existing = self.find_user(email)
            except Exception:
                try:
                    self.update_user_email("admin", moved_admin_email)
                except RuntimeError:
                    pass
                raise
            desired_user_id = existing.id if existing else desired_user_id

        if existing and existing.id != desired_user_id:
            previous_groups = self.user_group_names(existing.id)
            previous_id = existing.id
            previous_email = existing.email
            previous_uid = self.get_user_attribute(previous_id, "uidNumber")
            previous_gid = self.get_user_attribute(previous_id, "gidNumber")
            previous_home = self.get_user_attribute(previous_id, "homeDirectory")
            temporary_email = f"{previous_id}@migrated.invalid"
            self.update_user_email(previous_id, temporary_email)
            created: LLDAPUser | None = None
            try:
                created = self.create_user(
                    email,
                    display_name=existing.display_name or desired_user_id,
                    user_id=desired_user_id,
                    posix_uid=int(previous_uid) if previous_uid is not None else None,
                    posix_gid=int(previous_gid) if previous_gid is not None else None,
                    posix_home=previous_home,
                )
                self.set_password(created.id, password)
                for line in self.ensure_groups(created.id, sorted(set(previous_groups).union(groups or []))):
                    logs.append(f"LLDAP: {line}")
                logs.extend(self.ensure_user_posix(created.id))
                self.delete_user(previous_id)
            except Exception:
                if created is not None:
                    try:
                        self.delete_user(created.id)
                    except RuntimeError:
                        pass
                try:
                    self.update_user_email(previous_id, previous_email)
                except RuntimeError:
                    pass
                raise
            logs.append(f"LLDAP: migrated owner username {previous_id} to {created.id}")
            logs.append(f"LLDAP: password updated for {created.id}")
            return logs

        created = None
        try:
            if existing:
                desired_user_id = existing.id
                logs.append(f"LLDAP: user {desired_user_id} already exists")
            else:
                created = self.create_user(email, user_id=desired_user_id)
                desired_user_id = created.id
                logs.append(f"LLDAP: created user {desired_user_id} ({email})")

            self.set_password(desired_user_id, password)
            logs.append(f"LLDAP: password updated for {desired_user_id}")

            for line in self.ensure_groups(desired_user_id, groups or []):
                logs.append(f"LLDAP: {line}")
            logs.extend(self.ensure_user_posix(desired_user_id))
        except Exception:
            if created is not None:
                try:
                    self.delete_user(created.id)
                except RuntimeError:
                    pass
            if moved_admin_email is not None:
                try:
                    self.update_user_email("admin", moved_admin_email)
                except RuntimeError:
                    pass
            raise
        return logs

    def ensure_groups(self, user_id: str, group_names: list[str]) -> list[str]:
        logs: list[str] = []
        data = self._graphql("{ groups { id displayName users { id } } }")
        by_name = {g.get("displayName"): g for g in data.get("groups") or []}
        for gname in group_names:
            group = by_name.get(gname)
            if not group:
                logs.append(f"group {gname} not found")
                continue
            members = {u.get("id") for u in group.get("users") or []}
            if user_id in members:
                continue
            self._graphql(
                "mutation Add($u: String!, $g: Int!) { addUserToGroup(userId: $u, groupId: $g) { ok } }",
                {"u": user_id, "g": group.get("id")},
            )
            logs.append(f"added {user_id} to {gname}")
        return logs
