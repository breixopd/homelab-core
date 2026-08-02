#!/usr/bin/env python3
"""Capture deterministic README screenshots from the real Web UI templates."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
from toolkit.controller.read_models import (
    BootstrapCategory,
    BootstrapService,
    BootstrapSessionGrant,
    BootstrapStatus,
    BootstrapView,
)


class PreviewController:
    def bootstrap_status(self) -> BootstrapStatus:
        return BootstrapStatus(phase="uninitialized", has_active_capability=True)

    def exchange_bootstrap_capability(self, _token: str) -> BootstrapSessionGrant:
        return BootstrapSessionGrant(
            session_token="00000000-0000-4000-8000-000000000000.preview-session-token",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    def bootstrap_view(self, _token: str) -> BootstrapView:
        return BootstrapView(
            status=BootstrapStatus(phase="uninitialized", has_active_session=True),
            categories=[
                BootstrapCategory(
                    name="management",
                    label="Management",
                    description="Monitoring, backups, DNS, identity, and operations.",
                    node="infra",
                    service_count=1,
                    services=[BootstrapService(name="grafana", label="Grafana")],
                ),
                BootstrapCategory(
                    name="media",
                    label="Media",
                    description="Streaming, library automation, and caching.",
                    node="media",
                    service_count=1,
                    services=[BootstrapService(name="jellyfin", label="Jellyfin")],
                ),
            ],
        )

    def close(self) -> None:
        return None


def _serve(app, server: uvicorn.Server) -> None:
    server.run()


def main() -> None:
    output = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "WEBUI_SESSION_SECRET": "readme-screenshot-session-secret",
            "WEBUI_SECURE_COOKIES": "false",
            "HOMELAB_UI_WIZARD": "1",
        }
    )

    with tempfile.TemporaryDirectory(prefix="homelab-screenshots-") as temporary:
        import toolkit.webui.app as webui_app

        webui_app.controller_client_from_environment = PreviewController  # type: ignore[assignment]
        app = webui_app.create_app(root=Path(temporary))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning"))
        thread = threading.Thread(target=_serve, args=(app, server), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("preview server did not start")

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
                page.goto("http://127.0.0.1:8765/setup", wait_until="networkidle")
                page.locator('input[name="capability"]').fill(
                    "10000000-0000-4000-8000-000000000000.preview-capability-token"
                )
                with page.expect_navigation(wait_until="networkidle"):
                    page.get_by_role("button", name="Authorize setup").click()
                mode_heading = page.get_by_text("How will Homelab run?")
                if mode_heading.count() != 1:
                    raise RuntimeError(f"setup preview did not render at {page.url}: {page.content()[:1000]}")
                mode_heading.wait_for()
                page.screenshot(path=output / "setup.png", full_page=True)

                page.goto("https://auth.breixopd.space/", wait_until="networkidle")
                page.wait_for_timeout(1000)
                page.screenshot(path=output / "sign-in.png", full_page=True)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
