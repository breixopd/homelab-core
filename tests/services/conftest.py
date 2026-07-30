"""Shared fixtures for explicitly requested service-owned test suites.

Service tests are never collected by the framework CI paths. Reuse the
framework's deterministic transport/capability fixtures when a maintainer
chooses to run one service suite directly.
"""

pytest_plugins = ("tests.framework.toolkit.conftest",)
