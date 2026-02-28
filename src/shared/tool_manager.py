"""Tool discovery, loading, and registration."""

from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import Any, get_type_hints

import structlog
from click.testing import CliRunner
from fastapi import APIRouter, Depends, Request
from toon_format import encode as toon_encode
from typer.main import get_command

from api.deps import verify_api_key
from shared.tool_sdk import ToolContext, reset_tool_context, set_tool_context

log = structlog.get_logger()


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


def _to_toon(data: Any) -> str:
    """Encode data as TOON for token-efficient LLM responses, falling back to JSON."""
    try:
        return toon_encode(data)
    except Exception:
        return json.dumps(data, default=str)

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
    if (origin is types.UnionType or (origin is not None and str(origin) == "typing.Union")) and args:
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


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores comments and blank lines."""
    secrets: dict[str, str] = {}
    if not path.exists():
        return secrets
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        secrets[k.strip()] = v.strip()
    return secrets


class ToolManager:
    def __init__(
        self,
        tools_dir: Path,
        root_env_path: Path | None = None,
    ):
        self.tools_dir = tools_dir
        self.tools: dict[str, LoadedTool] = {}
        self._reload_lock = threading.Lock()
        # Load root .env once — all tools inherit these secrets
        self._root_secrets: dict[str, str] = {}
        if root_env_path is None:
            # Default: .env at the repo root (parent of tools_dir)
            root_env_path = tools_dir.parent / ".env"
        self._root_secrets = _load_env_file(root_env_path)

    def _collect_tools(self, enabled: set[str] | None) -> list[tuple[Path, dict]]:
        """Read pyproject.toml from each tool dir, optionally filtering."""
        tools = []
        for tool_dir in sorted(self.tools_dir.iterdir()):
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
            tools.append((tool_dir, meta))
        return tools

    def discover(
        self,
        only_names: set[str] | None = None,
    ) -> list[LoadedTool]:
        """Discover and load all tools."""
        if not self.tools_dir.exists():
            log.info("tools_dir_missing", path=str(self.tools_dir))
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
        for tool_dir, meta in tool_entries:
            try:
                lt = self._load_tool(tool_dir, meta)
                if lt:
                    loaded.append(lt)
            except Exception as exc:
                log.warning(
                    "tool_load_failed",
                    tool=meta.get("name", tool_dir.name),
                    error=str(exc),
                )

        self.tools = {p.name: p for p in loaded}
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

        # Build secrets: root .env (base) → tool .env (override)
        secrets: dict[str, str] = dict(self._root_secrets)
        tool_secrets = _load_env_file(tool_dir / ".env")
        secrets.update(tool_secrets)

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
        spec.loader.exec_module(module)

        # Inject secrets into os.environ so _client() factories using
        # os.getenv() can find them, then restore afterwards.
        original_env: dict[str, str | None] = {}
        for key, value in secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Set tool context so _client() factories can call secret()
        token = set_tool_context(ctx)
        try:
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
                            problems.append(
                                f"{tool_schema['name']}.{pname}: raw type '{ptype}'"
                            )
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
                    "methods": [m.method_name for m in sorted(lt.methods, key=lambda m: m.method_name)],
                }
            )
        return items

    def describe_tool(self, tool_name: str) -> dict[str, Any]:
        """Return full method schemas for a tool's methods."""
        lt = self.tools.get(tool_name)
        if not lt:
            return {"error": f"Tool '{tool_name}' not found"}
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
            return json.dumps({"error": f"Tool '{tool_name}' not found"})

        method = next((m for m in lt.methods if m.method_name == method_name), None)
        if not method:
            return json.dumps(
                {"error": f"Method '{method_name}' not found in tool '{tool_name}'"}
            )

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
                {"error": f"Tool called sys.exit({e.code})", "tool": tool_name, "method": method_name}
            )
        except Exception as e:
            return json.dumps(
                {"error": str(e), "tool": tool_name, "method": method_name}
            )
        finally:
            reset_tool_context(token)

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
        async def list_tools() -> dict:
            return {
                name: {
                    "description": p.description,
                    "methods": [m.method_name for m in p.methods],
                }
                for name, p in pm.tools.items()
            }

        @router.get("/{tool_name}")
        async def describe_tool(tool_name: str) -> dict:
            return pm.describe_tool(tool_name)

        @router.post("/{tool_name}/{method_name}")
        async def call_tool(tool_name: str, method_name: str, request: Request) -> dict:
            body = await request.json() if await request.body() else {}
            result = await pm.call_tool(tool_name, method_name, body)
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


