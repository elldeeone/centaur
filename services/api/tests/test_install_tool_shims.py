from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_TOOL_SHIMS = REPO_ROOT / "services" / "sandbox" / "install_tool_shims.py"


def _load_install_tool_shims():
    spec = importlib.util.spec_from_file_location("install_tool_shims", INSTALL_TOOL_SHIMS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_company_context_is_discoverable_by_centaur_tools() -> None:
    installer = _load_install_tool_shims()

    scripts = installer._discover_scripts([REPO_ROOT / "tools"])

    assert scripts["company_context"] == {
        "name": "company_context",
        "project_dir": str(REPO_ROOT / "tools" / "productivity" / "company_context"),
        "package": "company_context",
        "entrypoint": "company_context.cli:app",
        "client_module": "client.py",
    }
