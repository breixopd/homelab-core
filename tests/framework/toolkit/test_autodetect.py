"""Tests for toolkit/core/autodetect.py"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import Mock, patch

from toolkit.core.infra import autodetect


def make_completed_proc(stdout="", stderr="", returncode=0):
    proc = Mock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


class TestDetectTimezone:
    def test_tz_env_var_is_returned(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("os.readlink", side_effect=OSError),
        ):
            assert autodetect.detect_timezone() == "America/New_York"

    def test_etc_timezone_file(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with patch("toolkit.core.infra.autodetect.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = "Europe/London\n"
            with (
                patch("subprocess.run", side_effect=FileNotFoundError),
                patch("os.readlink", side_effect=OSError),
            ):
                assert autodetect.detect_timezone() == "Europe/London"

    def test_timedatectl_success(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch(
                "subprocess.run",
                return_value=make_completed_proc(stdout="Asia/Tokyo\n", returncode=0),
            ),
            patch("os.readlink", side_effect=OSError),
        ):
            assert autodetect.detect_timezone() == "Asia/Tokyo"

    def test_timedatectl_empty_output_falls_through(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch(
                "subprocess.run",
                return_value=make_completed_proc(stdout="\n", returncode=0),
            ),
            patch("os.readlink", side_effect=OSError),
        ):
            assert autodetect.detect_timezone() == "UTC"

    def test_timedatectl_nonzero_returncode_falls_through(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch(
                "subprocess.run",
                return_value=make_completed_proc(stdout="", returncode=1),
            ),
            patch("os.readlink", side_effect=OSError),
        ):
            assert autodetect.detect_timezone() == "UTC"

    def test_localtime_symlink_parsed(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("os.readlink", return_value="/usr/share/zoneinfo/Australia/Sydney"),
        ):
            assert autodetect.detect_timezone() == "Australia/Sydney"

    def test_localtime_symlink_no_zoneinfo_falls_through(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("os.readlink", return_value="/some/other/path"),
        ):
            assert autodetect.detect_timezone() == "UTC"

    def test_all_methods_fail_returns_utc(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        with (
            patch("toolkit.core.infra.autodetect.open", side_effect=OSError),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("os.readlink", side_effect=OSError),
        ):
            assert autodetect.detect_timezone() == "UTC"


class TestDetectGateway:
    def test_finds_gateway_on_private_subnet(self):
        output = "default via 10.10.10.1 dev eth0 proto dhcp\n"
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout=output, returncode=0),
        ):
            assert autodetect.detect_gateway("10.10.10") == "10.10.10.1"

    def test_ignores_gateway_outside_private_subnet(self):
        output = "default via 192.168.1.1 dev eth0\n"
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout=output, returncode=0),
        ):
            assert autodetect.detect_gateway("10.10.10") == "10.10.10.1"

    def test_subprocess_nonzero_returncode(self):
        with patch("subprocess.run", return_value=make_completed_proc(stdout="", returncode=1)):
            assert autodetect.detect_gateway("10.10.10") == "10.10.10.1"

    def test_subprocess_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ip", 5)):
            assert autodetect.detect_gateway("10.10.10") == "10.10.10.1"

    def test_file_not_found_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert autodetect.detect_gateway("172.16.0") == "172.16.0.1"

    def test_empty_stdout(self):
        with patch("subprocess.run", return_value=make_completed_proc(stdout="", returncode=0)):
            assert autodetect.detect_gateway("10.10.10") == "10.10.10.1"

    def test_multi_line_output_with_correct_gateway(self):
        output = (
            "default via 192.168.1.1 dev eth0 proto dhcp\n"
            "10.10.10.0/24 dev eth0 proto kernel scope link src 10.10.10.50\n"
        )
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout=output, returncode=0),
        ):
            assert autodetect.detect_gateway("10.10.10") == "10.10.10.1"


class TestDetectHwTranscoding:
    def test_nvidia_dev_files_exist(self):
        nvidia_paths = {"/dev/nvidiactl", "/dev/nvidia0"}
        with patch("os.path.exists") as mock_exists:
            mock_exists.side_effect = lambda path: path in nvidia_paths
            result = autodetect.detect_hw_transcoding()
            assert result == "nvidia"

    def test_nvidia_smi_succeeds(self):
        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = False
            with patch(
                "subprocess.run",
                return_value=make_completed_proc(stdout="NVIDIA GeForce RTX 3080\n", returncode=0),
            ):
                result = autodetect.detect_hw_transcoding()
                assert result == "nvidia"

    def test_nvidia_smi_nonzero(self):
        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = False
            with patch(
                "subprocess.run",
                return_value=make_completed_proc(stdout="", returncode=1),
            ):
                with patch("os.listdir") as mock_listdir:
                    mock_listdir.return_value = []
                    result = autodetect.detect_hw_transcoding()
                    assert result == "none"

    def test_vaapi_dri_exists(self):
        with patch("os.path.exists") as mock_exists:
            mock_exists.side_effect = lambda p: p == "/dev/dri"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                with patch("os.listdir") as mock_listdir:
                    mock_listdir.return_value = ["renderD128", "card0"]
                    result = autodetect.detect_hw_transcoding()
                    assert result == "vaapi"

    def test_no_hardware_returns_none(self):
        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = False
            with patch("subprocess.run", side_effect=FileNotFoundError):
                with patch("os.listdir") as mock_listdir:
                    mock_listdir.return_value = []
                    result = autodetect.detect_hw_transcoding()
                    assert result == "none"

    def test_nvidia_smi_timeout(self):
        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = False
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 5)):
                with patch("os.listdir") as mock_listdir:
                    mock_listdir.return_value = []
                    result = autodetect.detect_hw_transcoding()
                    assert result == "none"

    def test_nvidia_smi_empty_stdout(self):
        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = False
            with patch(
                "subprocess.run",
                return_value=make_completed_proc(stdout="\n", returncode=0),
            ):
                with patch("os.listdir") as mock_listdir:
                    mock_listdir.return_value = []
                    result = autodetect.detect_hw_transcoding()
                    assert result == "none"


class TestDetectPublicIp:
    def test_first_service_returns_valid_ipv4(self):
        response_mock = Mock()
        response_mock.__enter__ = Mock(return_value=response_mock)
        response_mock.__exit__ = Mock(return_value=None)
        response_mock.read.return_value = b"203.0.113.50\n"

        with patch("urllib.request.urlopen", return_value=response_mock) as mock_urlopen:
            result = autodetect.detect_public_ip()
            assert result == "203.0.113.50"
            mock_urlopen.assert_called_once()
            assert "api.ipify.org" in mock_urlopen.call_args[0][0]

    def test_first_service_fails_second_succeeds(self):
        import urllib.error

        response_mock = Mock()
        response_mock.__enter__ = Mock(return_value=response_mock)
        response_mock.__exit__ = Mock(return_value=None)
        response_mock.read.return_value = b"198.51.100.10\n"

        def urlopen_side_effect(url, **kwargs):
            if "ipify" in url:
                raise urllib.error.URLError("connection refused")
            return response_mock

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            result = autodetect.detect_public_ip()
            assert result == "198.51.100.10"

    def test_all_services_fail_returns_empty_string(self):
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("all failed"),
        ):
            assert autodetect.detect_public_ip() == ""

    def test_response_is_ipv6_falls_through(self):
        import urllib.error

        response_mock = Mock()
        response_mock.__enter__ = Mock(return_value=response_mock)
        response_mock.__exit__ = Mock(return_value=None)
        response_mock.read.return_value = b"2001:db8::1\n"

        def urlopen_side_effect(url, **kwargs):
            if "ipify" in url:
                return response_mock
            raise urllib.error.URLError("no more services")

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            result = autodetect.detect_public_ip()
            assert result == ""

    def test_response_invalid_ip_falls_through(self):
        import urllib.error

        response_mock = Mock()
        response_mock.__enter__ = Mock(return_value=response_mock)
        response_mock.__exit__ = Mock(return_value=None)
        response_mock.read.return_value = b"not-an-ip\n"

        def urlopen_side_effect(url, **kwargs):
            if "ipify" in url:
                return response_mock
            raise urllib.error.URLError("no more services")

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            result = autodetect.detect_public_ip()
            assert result == ""

    def test_oserror_on_all_services(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=OSError("network unreachable"),
        ):
            assert autodetect.detect_public_ip() == ""

    def test_timeout_on_all_services(self):
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            assert autodetect.detect_public_ip() == ""


class TestDetectSshPublicKey:
    def test_ed25519_key_found(self, tmp_path, monkeypatch):
        key_path = tmp_path / ".ssh" / "id_ed25519.pub"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAfakekey test@example.com\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert content == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAfakekey test@example.com"
            assert path == str(key_path)

    def test_ecdsa_key_found(self, tmp_path, monkeypatch):
        key_path = tmp_path / ".ssh" / "id_ecdsa.pub"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAfakekey\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert path == str(key_path)

    def test_rsa_key_found(self, tmp_path, monkeypatch):
        key_path = tmp_path / ".ssh" / "id_rsa.pub"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDFakeKey test@example.com\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert path == str(key_path)

    def test_ed25519_not_found_falls_through_to_ecdsa(self, tmp_path, monkeypatch):
        ecdsa_path = tmp_path / ".ssh" / "id_ecdsa.pub"
        ecdsa_path.parent.mkdir(parents=True, exist_ok=True)
        ecdsa_path.write_text("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAfakekey\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert path == str(ecdsa_path)

    def test_ed25519_and_ecdsa_missing_rsa_found(self, tmp_path, monkeypatch):
        rsa_path = tmp_path / ".ssh" / "id_rsa.pub"
        rsa_path.parent.mkdir(parents=True, exist_ok=True)
        rsa_path.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDFakeKey test@example.com\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert path == str(rsa_path)

    def test_no_keys_returns_empty_strings(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert content == ""
            assert path == ""

    def test_file_read_error_skips_to_next(self, tmp_path, monkeypatch):
        ecdsa_path = tmp_path / ".ssh" / "id_ecdsa.pub"
        ecdsa_path.parent.mkdir(parents=True, exist_ok=True)
        ecdsa_path.write_text("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAfakekey\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert path == str(ecdsa_path)

    def test_file_with_invalid_content_returns_empty(self, tmp_path, monkeypatch):
        rsa_path = tmp_path / ".ssh" / "id_rsa.pub"
        rsa_path.parent.mkdir(parents=True, exist_ok=True)
        rsa_path.write_text("not-a-valid-ssh-key-format\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            content, path = autodetect.detect_ssh_public_key()
            assert content == ""
            assert path == ""


class TestDetectDockerAvailable:
    def test_docker_info_succeeds(self):
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout="...", returncode=0),
        ):
            assert autodetect.detect_docker_available() is True

    def test_docker_info_nonzero_returncode(self):
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout="permission denied", returncode=1),
        ):
            assert autodetect.detect_docker_available() is False

    def test_docker_not_installed_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert autodetect.detect_docker_available() is False

    def test_docker_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            assert autodetect.detect_docker_available() is False


class TestDetectComposeAvailable:
    def test_compose_version_succeeds(self):
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout="Docker Compose version 2.23.0\n", returncode=0),
        ):
            assert autodetect.detect_compose_available() is True

    def test_compose_version_nonzero(self):
        with patch(
            "subprocess.run",
            return_value=make_completed_proc(stdout="command not found", returncode=127),
        ):
            assert autodetect.detect_compose_available() is False

    def test_compose_not_installed_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert autodetect.detect_compose_available() is False

    def test_compose_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            assert autodetect.detect_compose_available() is False


class TestDetectUidGid:
    def test_root_uid_gid_maps_container_1000_on_lxc_host(self, monkeypatch):
        """When running as root on an unprivileged LXC host, map container UID 1000."""
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(os, "getgid", lambda: 0)
        uid, gid = autodetect.detect_uid_gid()
        assert uid == autodetect.host_uid_for_container(1000)
        assert gid == autodetect.host_uid_for_container(1000)

    def test_root_with_nonzero_gid_still_maps_container_1000(self, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(os, "getgid", lambda: 50)
        uid, gid = autodetect.detect_uid_gid()
        assert uid == autodetect.host_uid_for_container(1000)
        assert gid == autodetect.host_uid_for_container(1000)

    def test_non_root_returns_actual_uid_gid(self, monkeypatch):
        """Non-root user gets their actual UID/GID."""
        monkeypatch.setattr(os, "getuid", lambda: 1001)
        monkeypatch.setattr(os, "getgid", lambda: 1001)
        uid, gid = autodetect.detect_uid_gid()
        assert uid == 1001
        assert gid == 1001

    def test_non_root_different_uid_gid(self, monkeypatch):
        """Non-root with mismatched UID/GID returns them as-is."""
        monkeypatch.setattr(os, "getuid", lambda: 1500)
        monkeypatch.setattr(os, "getgid", lambda: 150)
        uid, gid = autodetect.detect_uid_gid()
        assert uid == 1500
        assert gid == 150


class TestDetectComposeUidGid:
    def test_root_returns_container_namespace_1000(self, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(os, "getgid", lambda: 0)
        assert autodetect.detect_compose_uid_gid() == (1000, 1000)

    def test_non_root_returns_actual(self, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 1500)
        monkeypatch.setattr(os, "getgid", lambda: 150)
        assert autodetect.detect_compose_uid_gid() == (1500, 150)


class TestCheckVmReachable:
    def test_socket_connection_succeeds(self):
        mock_sock = Mock()
        mock_sock.__enter__ = Mock(return_value=mock_sock)
        mock_sock.__exit__ = Mock(return_value=None)

        with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
            result = autodetect.check_vm_reachable("10.10.10.50", port=22, timeout=2.0)
            assert result is True
            mock_conn.assert_called_once_with(("10.10.10.50", 22), timeout=2.0)

    def test_socket_connection_custom_port(self):
        mock_sock = Mock()
        mock_sock.__enter__ = Mock(return_value=mock_sock)
        mock_sock.__exit__ = Mock(return_value=None)

        with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
            result = autodetect.check_vm_reachable("10.10.10.50", port=2222, timeout=5.0)
            assert result is True
            mock_conn.assert_called_once_with(("10.10.10.50", 2222), timeout=5.0)

    def test_socket_connection_refused(self):
        with patch("socket.create_connection", side_effect=ConnectionRefusedError):
            assert autodetect.check_vm_reachable("10.10.10.50") is False

    def test_socket_connection_timeout(self):
        with patch("socket.create_connection", side_effect=TimeoutError):
            assert autodetect.check_vm_reachable("10.10.10.50") is False

    def test_socket_os_error(self):
        with patch("socket.create_connection", side_effect=OSError("network unreachable")):
            assert autodetect.check_vm_reachable("10.10.10.50") is False

    def test_default_port_is_22(self):
        mock_sock = Mock()
        mock_sock.__enter__ = Mock(return_value=mock_sock)
        mock_sock.__exit__ = Mock(return_value=None)

        with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
            autodetect.check_vm_reachable("10.10.10.50")
            mock_conn.assert_called_once_with(("10.10.10.50", 22), timeout=5)

    def test_default_timeout_is_5(self):
        mock_sock = Mock()
        mock_sock.__enter__ = Mock(return_value=mock_sock)
        mock_sock.__exit__ = Mock(return_value=None)

        with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
            autodetect.check_vm_reachable("10.10.10.50")
            _, kwargs = mock_conn.call_args
            assert kwargs["timeout"] == 5
