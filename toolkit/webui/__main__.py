from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    import uvicorn

    from toolkit.webui.app import create_app

    root = Path(os.environ["HOMELAB_ROOT"]) if "HOMELAB_ROOT" in os.environ else None
    port = int(os.environ.get("HOMELAB_UI_PORT", "8080"))
    app = create_app(root=root)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ in {"__main__", "__mp_main__"}:
    main()
