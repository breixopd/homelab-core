from __future__ import annotations

from toolkit.core.config.config import Config, NetworkConfig
from toolkit.core.manifest.networking import compile_network_listeners


def test_compile_network_listeners_resolves_plugin_owned_mail_and_dns_sources() -> None:
    listeners = compile_network_listeners(Config())

    mail = [listener for listener in listeners if listener.service == "mailserver"]
    dns = [listener for listener in listeners if listener.service == "adguard"]

    public_mail = [listener for listener in mail if listener.listener_id.endswith("-public")]
    internal_mail = [listener for listener in mail if listener.listener_id.endswith("-internal")]
    public_dns = [listener for listener in dns if listener.listener_id.endswith("-public")]
    internal_dns = [listener for listener in dns if listener.listener_id.endswith("-internal")]

    assert {(listener.port, listener.protocol) for listener in public_mail} == {
        (25, "tcp"),
        (465, "tcp"),
        (587, "tcp"),
        (993, "tcp"),
    }
    assert {(listener.port, listener.protocol) for listener in internal_mail} == {
        (25, "tcp"),
        (465, "tcp"),
        (587, "tcp"),
        (993, "tcp"),
        (4190, "tcp"),
    }
    assert all(listener.sources == ("any",) for listener in public_mail)
    assert all(listener.sources == ("10.10.10.0/24", "100.64.0.0/10") for listener in internal_mail)
    assert {(listener.port, listener.protocol) for listener in public_dns} == {(53, "tcp"), (53, "udp")}
    assert {(listener.port, listener.protocol) for listener in internal_dns} == {(53, "tcp"), (53, "udp")}
    assert all(listener.sources == ("any",) for listener in public_dns)
    assert all(listener.sources == ("10.10.10.0/24", "100.64.0.0/10") for listener in internal_dns)


def test_compile_network_listeners_removes_only_public_source_when_internet_is_disabled() -> None:
    cfg = Config(network=NetworkConfig(expose_via_internet=False))

    listeners = compile_network_listeners(cfg)

    exposed = [listener for listener in listeners if listener.service in {"mailserver", "adguard"}]
    assert exposed
    assert all(listener.listener_id.endswith("-internal") for listener in exposed)
    assert all(listener.sources == ("10.10.10.0/24", "100.64.0.0/10") for listener in exposed)


def test_compile_network_listeners_obeys_listener_predicates() -> None:
    cfg = Config(network=NetworkConfig(mail_public_access=False, dns_public_access=False))

    listeners = compile_network_listeners(cfg)

    exposed = [listener for listener in listeners if listener.service in {"mailserver", "adguard"}]
    assert exposed
    assert all(listener.listener_id.endswith("-internal") for listener in exposed)
    assert not any("any" in listener.sources for listener in exposed)


def test_ntfy_accepts_only_the_declared_servarr_node() -> None:
    listeners = compile_network_listeners(Config())
    ntfy = next(listener for listener in listeners if listener.service == "ntfy")

    assert ntfy.listener_id == "servarr-api"
    assert ntfy.port == 8090
    assert ntfy.sources == ("10.10.10.11",)
