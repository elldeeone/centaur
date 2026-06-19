import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


HOST_PATH = Path(__file__).resolve().parents[1] / "workflow_host.py"
SPEC = importlib.util.spec_from_file_location("workflow_host_under_test", HOST_PATH)
assert SPEC is not None
assert SPEC.loader is not None
workflow_host = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow_host
SPEC.loader.exec_module(workflow_host)


class ApiCompatModuleTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in list(sys.modules):
            if name == "api" or name.startswith("api."):
                sys.modules.pop(name, None)

    def test_workflow_engine_shim_preserves_real_api_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api_dir = Path(tmp) / "api"
            api_dir.mkdir()
            (api_dir / "__init__.py").write_text("", encoding="utf-8")
            (api_dir / "runtime_control.py").write_text(
                "VALUE = 'runtime-control-loaded'\n",
                encoding="utf-8",
            )
            sys.path.insert(0, tmp)
            sys.modules["api"] = types.ModuleType("api")
            try:
                workflow_host.install_api_compat_module()
                api_mod = importlib.import_module("api")
                runtime_control = importlib.import_module("api.runtime_control")
            finally:
                sys.path.remove(tmp)

        self.assertTrue(hasattr(api_mod, "__path__"))
        self.assertEqual(runtime_control.VALUE, "runtime-control-loaded")
        self.assertIs(api_mod.workflow_engine.WorkflowContext, workflow_host.WorkflowContext)


if __name__ == "__main__":
    unittest.main()
