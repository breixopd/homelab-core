from __future__ import annotations

import pytest
from defusedxml.common import DefusedXmlException
from toolkit.services.jellyfin.bootstrap import _reset_jellyfin_startup_wizard


def test_startup_wizard_reset_rejects_entity_declarations(tmp_path) -> None:
    config = tmp_path / "system.xml"
    config.write_text(
        '<!DOCTYPE Config [<!ENTITY done "true">]>'
        "<Config><IsStartupWizardCompleted>&done;</IsStartupWizardCompleted></Config>",
        encoding="utf-8",
    )

    with pytest.raises(DefusedXmlException):
        _reset_jellyfin_startup_wizard(tmp_path)
