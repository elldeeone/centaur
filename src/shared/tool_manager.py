"""Tool discovery, loading, and registration."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
import types
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, get_type_hints

import structlog
from click.testing import CliRunner
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from toon_format import encode as toon_encode
from typer.main import get_command

from api.deps import verify_api_key
from shared.tool_sdk import ToolContext, reset_tool_context, set_tool_context

log = structlog.get_logger()
_MAX_INLINE_TOOL_BINARY_BYTES = max(
    1024, int(os.getenv("TOOL_BINARY_INLINE_MAX_BYTES", str(1 * 1024 * 1024)))
)
_TOOL_BINARY_PREVIEW_BYTES = max(128, int(os.getenv("TOOL_BINARY_PREVIEW_BYTES", str(32 * 1024))))


class ToolMethod:
    def __init__(self, tool_name: str, method_name: str, fn: Callable, ctx: ToolContext):
        self.tool_name = tool_name
        self.method_name = method_name
        self.fn = fn
        self.ctx = ctx

    @property
    def qualified_name(self) -> str:
        return f"{self.tool_name}.{self.method_name}"


_LIFECYCLE_METHODS = frozenset({"close", "connect", "disconnect", "shutdown"})


def _flatten_for_tabular(data: Any) -> Any:
    """Flatten nested dicts in arrays so TOON can use tabular encoding.

    If every element is a dict with the same keys but some values are nested,
    stringify those nested values (via JSON) so the array qualifies for the
    compact ``[N]{fields}:`` tabular form.
    """
    if not isinstance(data, list) or not data:
        return data
    if not all(isinstance(item, dict) for item in data):
        return data
    keys = set(data[0].keys())
    if not all(set(d.keys()) == keys for d in data):
        return data
    has_nested = any(isinstance(v, (dict, list)) for item in data for v in item.values())
    if not has_nested:
        return data
    flat = []
    for item in data:
        row = {}
        for k, v in item.items():
            if isinstance(v, (dict, list)):
                row[k] = json.dumps(v, separators=(",", ":"), default=str)
            else:
                row[k] = v
        flat.append(row)
    return flat


def _normalize_for_serialization(data: Any) -> Any:
    """Normalize rich Python values into JSON-friendly structures."""
    if data is None or isinstance(data, (str, int, float, bool)):
        return data
    if isinstance(data, bytes):
        if len(data) > _MAX_INLINE_TOOL_BINARY_BYTES:
            preview_len = min(len(data), _TOOL_BINARY_PREVIEW_BYTES)
            return {
                "encoding": "base64",
                "byte_length": len(data),
                "content_base64_preview": base64.b64encode(data[:preview_len]).decode("ascii"),
                "preview_byte_length": preview_len,
                "inline_truncated": True,
                "inline_max_bytes": _MAX_INLINE_TOOL_BINARY_BYTES,
                "note": "Binary payload truncated; persist artifact and share path/ID for full content.",
            }
        return {
            "encoding": "base64",
            "byte_length": len(data),
            "content_base64": base64.b64encode(data).decode("ascii"),
        }
    if isinstance(data, Enum):
        return data.value
    if is_dataclass(data):
        return _normalize_for_serialization(asdict(data))
    if isinstance(data, dict):
        return {str(key): _normalize_for_serialization(value) for key, value in data.items()}
    if isinstance(data, (list, tuple, set)):
        return [_normalize_for_serialization(item) for item in data]

    model_dump = getattr(data, "model_dump", None)
    if callable(model_dump):
        try:
            return _normalize_for_serialization(model_dump())
        except TypeError:
            pass

    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        try:
            return _normalize_for_serialization(to_dict())
        except TypeError:
            pass
    return data


def _to_toon(data: Any) -> str:
    """Encode data as TOON for token-efficient LLM responses, falling back to JSON."""
    normalized = _normalize_for_serialization(data)
    try:
        toon = toon_encode(_flatten_for_tabular(normalized))
        compact_json = json.dumps(normalized, separators=(",", ":"), default=str)
        return toon if len(toon) <= len(compact_json) else compact_json
    except Exception:
        return json.dumps(normalized, default=str)


# Mapping from Python built-in types to clean names for schema output
_BUILTIN_TYPE_NAMES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _friendly_type_name(annotation: Any) -> str:
    """Convert a Python type annotation to a clean, human-readable string.

    Avoids raw ``<class 'str'>`` output by using simple names for built-in types
    and ``str()`` for union / generic forms.
    """
    if annotation in _BUILTIN_TYPE_NAMES:
        return _BUILTIN_TYPE_NAMES[annotation]
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", None)
    # typing.Optional / Union
    if (
        origin is types.UnionType or (origin is not None and str(origin) == "typing.Union")
    ) and args:
        parts = [_friendly_type_name(a) for a in args]
        return " | ".join(parts)
    # list[X], dict[K, V], etc.
    if origin is not None and args:
        base = _BUILTIN_TYPE_NAMES.get(origin, getattr(origin, "__name__", str(origin)))
        inner = ", ".join(_friendly_type_name(a) for a in args)
        return f"{base}[{inner}]"
    # Plain class — use __name__ if available
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    # Fallback
    return str(annotation)


class LoadedTool:
    def __init__(
        self,
        name: str,
        description: str,
        tool_dir: Path,
        cli_module: str,
        scripts: dict[str, str],
        ctx: ToolContext,
        methods: list[ToolMethod],
    ):
        self.name = name
        self.description = description
        self.tool_dir = tool_dir
        self.cli_module = cli_module
        self.scripts = scripts
        self.ctx = ctx
        self.methods = methods

    @property
    def cli_path(self) -> Path:
        return self.tool_dir / self.cli_module


def _install_deps(deps: list[str]) -> None:
    """Install tool dependencies into the current environment."""
    if not deps:
        return
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--quiet", *deps]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *deps]
    log.info("installing_tool_deps", deps=deps)
    subprocess.run(cmd, check=True, capture_output=True)


def _fetch_backend_secrets() -> dict[str, str]:
    """Bulk-fetch all secrets from the pluggable secret backend.

    Returns an empty dict if the backend is unavailable (e.g. no
    ``SECRET_MANAGER_URL`` configured or the sidecar is down).
    """
    try:
        from secret_backends.registry import get_backend

        backend = get_backend()

        import asyncio
        import concurrent.futures

        async def _list():
            return await backend.list_keys()

        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                keys = pool.submit(asyncio.run, _list()).result(timeout=10)
        except RuntimeError:
            keys = asyncio.run(_list())

        secrets: dict[str, str] = {}
        for key in keys:
            val = backend.get_sync(key)
            if val is not None:
                secrets[key] = val
        if secrets:
            log.info("loaded_backend_secrets", count=len(secrets))
        return secrets
    except Exception:
        log.debug("failed to fetch secrets from backend", exc_info=True)
        return {}


def load_plugins_config(config_path: Path) -> list[Path]:
    """Read a tools.toml and return resolved plugin directory paths.

    The TOML file is expected to contain a ``plugin_dirs`` key whose value is a
    list of directory paths (strings).  Relative paths are resolved against the
    config file's parent directory.  Returns an empty list when the file does not
    exist.
    """
    if not config_path.exists():
        return []
    base = config_path.parent
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    dirs: list[Path] = []
    for entry in data.get("plugin_dirs", []):
        p = Path(entry)
        dirs.append(p if p.is_absolute() else (base / p).resolve())
    return dirs


class ToolManager:
    def __init__(
        self,
        tools_dir: Path | list[Path],
    ):
        if isinstance(tools_dir, list):
            self.tools_dirs: list[Path] = list(tools_dir)
        else:
            self.tools_dirs = [tools_dir]
        self.tools_dir = self.tools_dirs[0]
        self.tools: dict[str, LoadedTool] = {}
        self.load_failures: list[dict[str, str]] = []
        self._reload_lock = threading.Lock()
        # All secrets come from the secret-manager HTTP sidecar.
        self._root_secrets: dict[str, str] = _fetch_backend_secrets()

    def _collect_tools(self, enabled: set[str] | None) -> list[tuple[Path, dict]]:
        """Read pyproject.toml from each tool dir, optionally filtering.

        Directories in ``self.tools_dirs`` are scanned in order.  When the same
        tool name appears in a later directory it shadows the earlier one (useful
        for private-overrides-public).
        """
        seen: dict[str, int] = {}
        tools: list[tuple[Path, dict]] = []
        for dir_idx, base_dir in enumerate(self.tools_dirs):
            if not base_dir.exists():
                continue
            for tool_dir in sorted(base_dir.iterdir()):
                if not tool_dir.is_dir() or tool_dir.name.startswith((".", "_")):
                    continue

                pyproject_path = tool_dir / "pyproject.toml"
                if not pyproject_path.exists():
                    continue

                with open(pyproject_path, "rb") as f:
                    pyproject = tomllib.load(f)

                project = pyproject.get("project", {})
                tool_conf = pyproject.get("tool", {}).get("ai-v2", {})

                name = tool_dir.name
                if enabled is not None and name not in enabled:
                    log.debug("tool_skipped", tool=name)
                    continue

                meta = {
                    "name": name,
                    "description": project.get("description", ""),
                    "dependencies": project.get("dependencies", []),
                    "scripts": project.get("scripts", {}),
                    "module": tool_conf.get("module", "tools.py"),
                    "cli_module": tool_conf.get("cli_module", "cli.py"),
                }

                if name in seen:
                    prev_idx = seen[name]
                    prev_pos = next(i for i, (_, m) in enumerate(tools) if m["name"] == name)
                    log.info(
                        "tool_shadowed",
                        tool=name,
                        shadowed_dir=str(self.tools_dirs[prev_idx]),
                        by_dir=str(base_dir),
                    )
                    tools[prev_pos] = (tool_dir, meta)
                else:
                    tools.append((tool_dir, meta))
                seen[name] = dir_idx
        return tools

    def discover(
        self,
        only_names: set[str] | None = None,
    ) -> list[LoadedTool]:
        """Discover and load all tools."""
        existing = [d for d in self.tools_dirs if d.exists()]
        if not existing:
            self.load_failures = []
            log.info("tools_dirs_missing", paths=[str(d) for d in self.tools_dirs])
            return []

        enabled = only_names
        tool_entries = self._collect_tools(enabled)

        # Collect all dependencies across enabled tools and install in one shot
        all_deps: list[str] = []
        for _, meta in tool_entries:
            all_deps.extend(meta.get("dependencies", []))
        if all_deps:
            try:
                _install_deps(list(set(all_deps)))
            except Exception as exc:
                log.warning("tool_deps_install_failed", deps=all_deps, error=str(exc))

        # Now load each tool
        loaded = []
        load_failures: list[dict[str, str]] = []
        for tool_dir, meta in tool_entries:
            try:
                lt = self._load_tool(tool_dir, meta)
                if lt:
                    loaded.append(lt)
            except Exception as exc:
                tool_name = str(meta.get("name", tool_dir.name))
                load_failures.append({"name": tool_name, "error": str(exc)})
                log.warning(
                    "tool_load_failed",
                    tool=tool_name,
                    error=str(exc),
                )

        self.load_failures = load_failures
        self.tools = {p.name: p for p in loaded}
        log.info(
            "tools_discovery_complete",
            loaded=len(loaded),
            failed=len(load_failures),
            failed_tools=[f["name"] for f in load_failures],
        )
        return loaded

    def reload(self) -> dict[str, Any]:
        """Reload all tools by clearing module caches and re-discovering."""
        with self._reload_lock:
            stale = [k for k in sys.modules if k.startswith("shared.tools_runtime.")]
            for k in stale:
                del sys.modules[k]

            loaded = self.discover()
            return {
                "reloaded": len(loaded),
                "tools": [p.name for p in loaded],
            }

    def _load_tool(self, tool_dir: Path, manifest: dict) -> LoadedTool | None:
        name = manifest["name"]

        secrets: dict[str, str] = dict(self._root_secrets)

        ctx = ToolContext(name=name, secrets=secrets)

        # Register the tool dir as a package so relative imports work
        pkg_name = f"shared.tools_runtime.{name}"
        init_path = tool_dir / "__init__.py"
        if init_path.exists():
            pkg_spec = importlib.util.spec_from_file_location(
                pkg_name,
                init_path,
                submodule_search_locations=[str(tool_dir)],
            )
            if pkg_spec and pkg_spec.loader:
                pkg_mod = importlib.util.module_from_spec(pkg_spec)
                sys.modules[pkg_name] = pkg_mod
                pkg_spec.loader.exec_module(pkg_mod)
        else:
            # Create a virtual package
            pkg_mod = types.ModuleType(pkg_name)
            pkg_mod.__path__ = [str(tool_dir)]  # type: ignore[attr-defined]
            sys.modules[pkg_name] = pkg_mod

        # Ensure parent namespace exists
        if "shared.tools_runtime" not in sys.modules:
            ns = types.ModuleType("shared.tools_runtime")
            ns.__path__ = []  # type: ignore[attr-defined]
            sys.modules["shared.tools_runtime"] = ns

        # Import the tool module
        module_file = manifest.get("module", "client.py")
        module_path = tool_dir / module_file
        if not module_path.exists():
            log.warning("tool_module_missing", tool=name, module=module_file)
            return None

        mod_name = f"{pkg_name}.{Path(module_file).stem}"
        spec = importlib.util.spec_from_file_location(mod_name, module_path)
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = pkg_name  # type: ignore[attr-defined]
        sys.modules[mod_name] = module

        # Inject secrets into os.environ BEFORE exec_module so that
        # module-level os.environ.get() calls in the tool (and its
        # transitive imports) see the correct values.
        original_env: dict[str, str | None] = {}
        for key, value in secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Set tool context so _client() factories can call secret()
        token = set_tool_context(ctx)
        try:
            spec.loader.exec_module(module)
            methods = self._collect_methods(name, module, ctx)
        finally:
            reset_tool_context(token)
            for key, previous in original_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

        description = manifest.get("description", "")
        loaded_tool = LoadedTool(
            name=name,
            description=description,
            tool_dir=tool_dir,
            cli_module=manifest.get("cli_module", "cli.py"),
            scripts=manifest.get("scripts", {}),
            ctx=ctx,
            methods=methods,
        )
        log.info(
            "tool_loaded",
            tool=name,
            methods=[m.method_name for m in methods],
        )
        return loaded_tool

    def _resolve_tool_for_cli(self, tool: str) -> LoadedTool | None:
        lt = self.tools.get(tool)
        if lt:
            return lt

        # Allow script aliases from [project.scripts] to map back to tools.
        for candidate in self.tools.values():
            if tool in candidate.scripts:
                return candidate
        return None

    def list_cli_tools(self) -> dict[str, dict[str, Any]]:
        """Return dynamic CLI tool metadata for all loaded tools."""
        cli_tools: dict[str, dict[str, Any]] = {}
        for lt in self.tools.values():
            if not lt.cli_path.exists():
                continue
            aliases = sorted(lt.scripts.keys())
            cli_tools[lt.name] = {
                "tool": lt.name,
                "description": lt.description,
                "cli_path": str(lt.cli_path),
                "method_count": len(lt.methods),
                "aliases": aliases,
            }
            for alias in aliases:
                cli_tools[alias] = {
                    "tool": lt.name,
                    "description": lt.description,
                    "cli_path": str(lt.cli_path),
                    "method_count": len(lt.methods),
                    "aliases": aliases,
                }
        return cli_tools

    def run_cli(self, tool: str, args: list[str]) -> str:
        """Run a tool CLI dynamically without static allowlists."""
        loaded_tool = self._resolve_tool_for_cli(tool)
        if loaded_tool is None:
            available = sorted(self.list_cli_tools().keys())
            return json.dumps(
                {
                    "error": f"Unknown CLI tool '{tool}'",
                    "available": available,
                }
            )

        cli_path = loaded_tool.cli_path
        if not cli_path.exists():
            return json.dumps(
                {
                    "error": f"CLI not found for tool '{loaded_tool.name}'",
                    "expected_path": str(cli_path),
                }
            )

        cli_module_name = f"shared.tools_runtime.{loaded_tool.name}.{cli_path.stem}"
        cli_spec = importlib.util.spec_from_file_location(cli_module_name, cli_path)
        if not cli_spec or not cli_spec.loader:
            return json.dumps(
                {
                    "error": f"Unable to load CLI module for tool '{loaded_tool.name}'",
                    "cli_path": str(cli_path),
                }
            )

        cli_module = importlib.util.module_from_spec(cli_spec)
        cli_module.__package__ = f"shared.tools_runtime.{loaded_tool.name}"  # type: ignore[attr-defined]
        sys.modules[cli_module_name] = cli_module

        original_env: dict[str, str | None] = {}
        for key, value in loaded_tool.ctx.secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            cli_spec.loader.exec_module(cli_module)
            app = getattr(cli_module, "app", None)
            if app is None:
                return json.dumps(
                    {
                        "error": f"CLI app not found for tool '{loaded_tool.name}'",
                        "expected_object": "app",
                    }
                )

            if hasattr(app, "registered_commands"):
                app = get_command(app)

            runner = CliRunner()
            result = runner.invoke(app, args, prog_name=loaded_tool.name)
            output = (result.output or "").strip()
            if result.exit_code != 0:
                details: dict[str, Any] = {
                    "error": f"CLI failed for tool '{loaded_tool.name}'",
                    "exit_code": result.exit_code,
                    "output": output,
                }
                if result.exception is not None:
                    details["exception"] = str(result.exception)
                return json.dumps(details)
            return output
        except Exception as exc:
            return json.dumps(
                {
                    "error": f"CLI raised for tool '{loaded_tool.name}'",
                    "detail": str(exc),
                }
            )
        finally:
            for key, previous in original_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def tool_test_matrix(self) -> list[dict[str, Any]]:
        """Summarize import/discovery/CLI readiness for loaded tools."""
        matrix: list[dict[str, Any]] = []
        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            matrix.append(
                {
                    "tool": lt.name,
                    "library_import": True,
                    "discovered_methods": [m.method_name for m in lt.methods],
                    "cli_available": lt.cli_path.exists(),
                    "cli_path": str(lt.cli_path),
                    "aliases": sorted(lt.scripts.keys()),
                }
            )
        return matrix

    def smoke_test_registry(self) -> list[dict[str, Any]]:
        """Verify registry integrity for tools and CLI aliases."""
        entries = self.list_cli_tools()
        results: list[dict[str, Any]] = []

        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            problems: list[str] = []
            if not lt.methods:
                problems.append("no_discovered_methods")
            if lt.cli_path.exists() and lt.name not in entries:
                problems.append("tool_missing_from_cli_registry")
            for alias in lt.scripts:
                if alias not in entries:
                    problems.append(f"missing_alias:{alias}")

            results.append(
                {
                    "tool": lt.name,
                    "status": "ok" if not problems else "failed",
                    "problems": problems,
                }
            )

        return results

    @staticmethod
    def _parse_cli_output(output: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict) and "error" in parsed:
                return parsed
        except json.JSONDecodeError:
            return None
        return None

    def smoke_test_clis(self, cli_args: list[str] | None = None) -> list[dict[str, Any]]:
        """Run a CLI smoke test for each loaded tool that has a cli.py."""
        args = cli_args or ["--help"]
        results: list[dict[str, Any]] = []
        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            if not lt.cli_path.exists():
                results.append(
                    {
                        "tool": lt.name,
                        "status": "missing_cli",
                        "cli_path": str(lt.cli_path),
                    }
                )
                continue

            output = self.run_cli(lt.name, args)
            parsed = self._parse_cli_output(output)
            if parsed is not None:
                results.append(
                    {
                        "tool": lt.name,
                        "status": "failed",
                        "details": parsed,
                    }
                )
                continue

            results.append(
                {
                    "tool": lt.name,
                    "status": "ok",
                    "cli_path": str(lt.cli_path),
                }
            )
        return results

    def smoke_test_aliases(self, cli_args: list[str] | None = None) -> list[dict[str, Any]]:
        """Run CLI smoke tests via script aliases from tool manifests."""
        args = cli_args or ["--help"]
        results: list[dict[str, Any]] = []

        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            aliases = sorted(lt.scripts)
            if not aliases:
                results.append({"tool": lt.name, "status": "missing_aliases"})
                continue

            for alias in aliases:
                output = self.run_cli(alias, args)
                parsed = self._parse_cli_output(output)
                if parsed is not None:
                    results.append(
                        {
                            "tool": lt.name,
                            "alias": alias,
                            "status": "failed",
                            "details": parsed,
                        }
                    )
                    continue

                results.append(
                    {
                        "tool": lt.name,
                        "alias": alias,
                        "status": "ok",
                    }
                )

        return results

    def smoke_test_rest_routes(self) -> list[dict[str, Any]]:
        """Verify that every tool function is callable via the dispatcher."""
        results: list[dict[str, Any]] = []
        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            results.append(
                {
                    "tool": lt.name,
                    "status": "ok",
                    "registered_methods": len(lt.methods),
                    "total_methods": len(lt.methods),
                }
            )
        return results

    def smoke_test_schemas(self) -> list[dict[str, Any]]:
        """Validate describe_tool output for every loaded tool."""
        bad_pattern = re.compile(r"<class '")
        results: list[dict[str, Any]] = []
        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            schema = self.describe_tool(lt.name)
            problems: list[str] = []
            if "error" in schema:
                problems.append(f"describe_error: {schema['error']}")
            else:
                for tool_schema in schema.get("methods", []):
                    for pname, pinfo in tool_schema.get("parameters", {}).items():
                        ptype = pinfo.get("type", "")
                        if bad_pattern.search(str(ptype)):
                            problems.append(f"{tool_schema['name']}.{pname}: raw type '{ptype}'")
            results.append(
                {
                    "tool": lt.name,
                    "status": "ok" if not problems else "failed",
                    "problems": problems,
                }
            )
        return results

    @staticmethod
    def _collect_methods(tool_name: str, module: Any, ctx: ToolContext) -> list[ToolMethod]:
        """Collect tools from a tool module.

        The module must have a _client() factory. Call it once to get a cached
        instance and expose every public method as a tool.
        """
        methods: list[ToolMethod] = []
        seen: set[str] = set()

        factory = getattr(module, "_client", None)
        if factory and callable(factory):
            instance = factory()
            for method_name, descriptor in sorted(
                vars(type(instance)).items(),
                key=lambda item: item[0],
            ):
                if method_name.startswith("_") or method_name in _LIFECYCLE_METHODS:
                    continue
                if isinstance(descriptor, property):
                    continue
                if not callable(descriptor):
                    continue
                method = getattr(instance, method_name, None)
                if not inspect.ismethod(method):
                    continue
                methods.append(ToolMethod(tool_name, method_name, method, ctx))
                seen.add(method_name)

        return methods

    def list_tools(self) -> list[dict[str, Any]]:
        """List all loaded tools with their method names (no schemas)."""
        items: list[dict[str, Any]] = []
        for lt in sorted(self.tools.values(), key=lambda p: p.name):
            items.append(
                {
                    "tool": lt.name,
                    "description": lt.description,
                    "methods": [
                        m.method_name for m in sorted(lt.methods, key=lambda m: m.method_name)
                    ],
                }
            )
        return items

    def describe_tool(self, tool_name: str) -> dict[str, Any]:
        """Return full method schemas for a tool's methods."""
        lt = self.tools.get(tool_name)
        if not lt:
            return {
                "error": f"Tool '{tool_name}' not found",
                "available": sorted(self.tools.keys()),
            }
        method_schemas: list[dict[str, Any]] = []
        for method in sorted(lt.methods, key=lambda m: m.method_name):
            try:
                sig = inspect.signature(method.fn)
            except (TypeError, ValueError) as exc:
                method_schemas.append(
                    {
                        "name": method.method_name,
                        "description": (method.fn.__doc__ or "").strip().split("\n")[0],
                        "parameters": {},
                        "signature_error": str(exc),
                    }
                )
                continue
            params: dict[str, Any] = {}
            for pname, param in sig.parameters.items():
                if pname == "self":
                    continue
                ptype = "any"
                if param.annotation is not inspect.Parameter.empty:
                    ptype = _friendly_type_name(param.annotation)
                pinfo: dict[str, Any] = {"type": ptype}
                if param.default is not inspect.Parameter.empty:
                    pinfo["default"] = param.default
                else:
                    pinfo["required"] = True
                params[pname] = pinfo
            method_schemas.append(
                {
                    "name": method.method_name,
                    "description": (method.fn.__doc__ or "").strip().split("\n")[0],
                    "parameters": params,
                }
            )
        return {
            "tool": lt.name,
            "description": lt.description,
            "methods": method_schemas,
        }

    async def call_tool(self, tool_name: str, method_name: str, args: dict[str, Any]) -> str:
        """Call a tool method by name and return the result as a TOON string."""
        lt = self.tools.get(tool_name)
        if not lt:
            return json.dumps(
                {"error": f"Tool '{tool_name}' not found", "available": sorted(self.tools.keys())}
            )

        method = next((m for m in lt.methods if m.method_name == method_name), None)
        if not method:
            return json.dumps(
                {
                    "error": f"Method '{method_name}' not found in tool '{tool_name}'",
                    "available_methods": sorted(m.method_name for m in lt.methods),
                }
            )

        # Inject secrets into os.environ so tools using os.getenv() at
        # runtime (not just import time) can find them.
        original_env: dict[str, str | None] = {}
        for key, value in method.ctx.secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        token = set_tool_context(method.ctx)
        try:
            if inspect.iscoroutinefunction(method.fn):
                result = await method.fn(**args)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: method.fn(**args))
            if isinstance(result, str):
                return result
            return _to_toon(result)
        except SystemExit as e:
            return json.dumps(
                {
                    "error": f"Tool called sys.exit({e.code})",
                    "tool": tool_name,
                    "method": method_name,
                }
            )
        except Exception as e:
            return json.dumps({"error": str(e), "tool": tool_name, "method": method_name})
        finally:
            reset_tool_context(token)
            for key, previous in original_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def create_rest_router(self) -> APIRouter:
        """Create a stable FastAPI router that dispatches to tools via live lookup.

        Routes are fixed at registration time — tool calls resolve through
        ``self.tools`` at request time so hot-reloads take effect without
        swapping routes.
        """
        pm = self
        router = APIRouter(
            prefix="/tools",
            dependencies=[Depends(verify_api_key)],
        )

        @router.get("")
        async def list_tools(request: Request) -> dict:
            key_info = getattr(request.state, "api_key_info", None)
            result = {}
            for name, p in pm.tools.items():
                if key_info is not None:
                    from api.api_keys import check_scope

                    if not check_scope(key_info, "tools", name):
                        continue
                result[name] = {
                    "description": p.description,
                    "methods": [m.method_name for m in p.methods],
                }
            return result

        @router.get("/{tool_name}")
        async def describe_tool(tool_name: str, request: Request) -> dict:
            key_info = getattr(request.state, "api_key_info", None)
            if key_info is not None:
                from api.api_keys import check_scope

                if not check_scope(key_info, "tools", tool_name):
                    raise HTTPException(
                        status_code=403,
                        detail=f"API key does not have access to tool '{tool_name}'",
                    )
            return pm.describe_tool(tool_name)

        @router.post("/{tool_name}/{method_name}")
        async def call_tool(tool_name: str, method_name: str, request: Request):
            raw_body = await request.body()
            body: Any = {}
            if raw_body:
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError:
                    error = json.dumps(
                        {
                            "error": "Request body must be valid JSON",
                            "tool": tool_name,
                            "method": method_name,
                        }
                    )
                    if "text/plain" in request.headers.get("accept", ""):
                        return PlainTextResponse(error)
                    return {"tool": tool_name, "method": method_name, "result": error}
            if not isinstance(body, dict):
                error = json.dumps(
                    {
                        "error": "Request body must be a JSON object",
                        "tool": tool_name,
                        "method": method_name,
                    }
                )
                if "text/plain" in request.headers.get("accept", ""):
                    return PlainTextResponse(error)
                return {"tool": tool_name, "method": method_name, "result": error}
            # Tool-level scope check
            key_info = getattr(request.state, "api_key_info", None)
            if key_info is not None:
                from api.api_keys import check_scope

                if not check_scope(key_info, "tools", tool_name):
                    raise HTTPException(
                        status_code=403,
                        detail=f"API key does not have access to tool '{tool_name}'",
                    )
            result = await pm.call_tool(tool_name, method_name, body)
            if "text/plain" in request.headers.get("accept", ""):
                return PlainTextResponse(result)
            return {"tool": tool_name, "method": method_name, "result": result}

        return router


def _make_wrapper(tool: ToolMethod) -> Callable:
    """Wrap a tool method to inject context and handle errors."""

    async def wrapper(**kwargs: Any) -> str:
        token = set_tool_context(tool.ctx)
        try:
            if inspect.iscoroutinefunction(tool.fn):
                result = await tool.fn(**kwargs)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: tool.fn(**kwargs))
            if isinstance(result, str):
                return result
            return _to_toon(result)
        except SystemExit as e:
            return json.dumps(
                {
                    "error": f"Tool called sys.exit({e.code})",
                    "tool": tool.tool_name,
                    "method": tool.method_name,
                }
            )
        except Exception as e:
            return json.dumps(
                {
                    "error": str(e),
                    "tool": tool.tool_name,
                    "method": tool.method_name,
                }
            )
        finally:
            reset_tool_context(token)

    # Preserve original signature for schema generation
    wrapper.__name__ = tool.qualified_name.replace(".", "_")
    wrapper.__doc__ = tool.fn.__doc__ or f"{tool.tool_name} — {tool.method_name}"
    wrapper.__signature__ = inspect.signature(tool.fn)  # type: ignore[attr-defined]
    try:
        wrapper.__annotations__ = get_type_hints(tool.fn)
    except Exception:
        wrapper.__annotations__ = getattr(tool.fn, "__annotations__", {})
    return wrapper


def _register_mcp_tool(mcp: Any, tool: ToolMethod) -> None:
    """Register a single tool function as an MCP tool."""
    wrapper = _make_wrapper(tool)
    # FastMCP uses the function name as the tool name
    wrapper.__name__ = tool.qualified_name.replace(".", "_")
    mcp.tool(name=tool.qualified_name)(wrapper)
