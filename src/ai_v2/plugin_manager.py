"""Plugin discovery, loading, and registration."""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_type_hints

import structlog
from click.testing import CliRunner
from fastapi import APIRouter
from pydantic import create_model

from .plugin_sdk import PluginContext, reset_plugin_context, set_plugin_context

log = structlog.get_logger()


class LoadedTool:
    def __init__(self, plugin_name: str, tool_name: str, fn: Callable, ctx: PluginContext):
        self.plugin_name = plugin_name
        self.tool_name = tool_name
        self.fn = fn
        self.ctx = ctx

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin_name}.{self.tool_name}"


_LIFECYCLE_METHODS = frozenset({"close", "connect", "disconnect", "shutdown"})


class LoadedPlugin:
    def __init__(
        self,
        name: str,
        description: str,
        plugin_dir: Path,
        cli_module: str,
        scripts: dict[str, str],
        ctx: PluginContext,
        tools: list[LoadedTool],
    ):
        self.name = name
        self.description = description
        self.plugin_dir = plugin_dir
        self.cli_module = cli_module
        self.scripts = scripts
        self.ctx = ctx
        self.tools = tools

    @property
    def cli_path(self) -> Path:
        return self.plugin_dir / self.cli_module


def _install_deps(deps: list[str]) -> None:
    """Install plugin dependencies into the current environment."""
    if not deps:
        return
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--quiet", *deps]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *deps]
    log.info("installing_plugin_deps", deps=deps)
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


class PluginManager:
    def __init__(
        self,
        plugins_dir: Path,
        root_env_path: Path | None = None,
    ):
        self.plugins_dir = plugins_dir
        self.plugins: dict[str, LoadedPlugin] = {}
        # Load root .env once — all plugins inherit these secrets
        self._root_secrets: dict[str, str] = {}
        if root_env_path is None:
            # Default: .env at the repo root (parent of plugins_dir)
            root_env_path = plugins_dir.parent / ".env"
        self._root_secrets = _load_env_file(root_env_path)

    def _collect_plugins(self, enabled: set[str] | None) -> list[tuple[Path, dict]]:
        """Read pyproject.toml from each plugin dir, optionally filtering."""
        plugins = []
        for plugin_dir in sorted(self.plugins_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith((".", "_")):
                continue

            pyproject_path = plugin_dir / "pyproject.toml"
            if not pyproject_path.exists():
                continue

            with open(pyproject_path, "rb") as f:
                pyproject = tomllib.load(f)

            project = pyproject.get("project", {})
            plugin_conf = pyproject.get("tool", {}).get("ai-v2-plugin", {})

            name = plugin_dir.name
            if enabled is not None and name not in enabled:
                log.debug("plugin_skipped", plugin=name)
                continue

            meta = {
                "name": name,
                "description": project.get("description", ""),
                "dependencies": project.get("dependencies", []),
                "scripts": project.get("scripts", {}),
                "module": plugin_conf.get("module", "tools.py"),
                "cli_module": plugin_conf.get("cli_module", "cli.py"),
            }
            plugins.append((plugin_dir, meta))
        return plugins

    def discover(
        self,
        only_plugins: set[str] | None = None,
    ) -> list[LoadedPlugin]:
        """Discover and load all plugins."""
        if not self.plugins_dir.exists():
            log.info("plugins_dir_missing", path=str(self.plugins_dir))
            return []

        enabled = only_plugins
        plugin_entries = self._collect_plugins(enabled)

        # Collect all dependencies across enabled plugins and install in one shot
        all_deps: list[str] = []
        for _, meta in plugin_entries:
            all_deps.extend(meta.get("dependencies", []))
        if all_deps:
            try:
                _install_deps(list(set(all_deps)))
            except Exception as exc:
                log.warning("plugin_deps_install_failed", deps=all_deps, error=str(exc))

        # Now load each plugin
        loaded = []
        for plugin_dir, meta in plugin_entries:
            try:
                plugin = self._load_plugin(plugin_dir, meta)
                if plugin:
                    loaded.append(plugin)
            except Exception as exc:
                log.warning(
                    "plugin_load_failed",
                    plugin=meta.get("name", plugin_dir.name),
                    error=str(exc),
                )

        self.plugins = {p.name: p for p in loaded}
        return loaded

    def _load_plugin(self, plugin_dir: Path, manifest: dict) -> LoadedPlugin | None:
        name = manifest["name"]

        # Build secrets: root .env (base) → plugin .env (override)
        secrets: dict[str, str] = dict(self._root_secrets)
        plugin_secrets = _load_env_file(plugin_dir / ".env")
        secrets.update(plugin_secrets)

        ctx = PluginContext(name=name, secrets=secrets)

        # Register the plugin dir as a package so relative imports work
        pkg_name = f"ai_v2.plugins_runtime.{name}"
        init_path = plugin_dir / "__init__.py"
        if init_path.exists():
            pkg_spec = importlib.util.spec_from_file_location(
                pkg_name,
                init_path,
                submodule_search_locations=[str(plugin_dir)],
            )
            if pkg_spec and pkg_spec.loader:
                pkg_mod = importlib.util.module_from_spec(pkg_spec)
                sys.modules[pkg_name] = pkg_mod
                pkg_spec.loader.exec_module(pkg_mod)
        else:
            # Create a virtual package
            import types

            pkg_mod = types.ModuleType(pkg_name)
            pkg_mod.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
            sys.modules[pkg_name] = pkg_mod

        # Ensure parent namespace exists
        if "ai_v2.plugins_runtime" not in sys.modules:
            import types as _t

            ns = _t.ModuleType("ai_v2.plugins_runtime")
            ns.__path__ = []  # type: ignore[attr-defined]
            sys.modules["ai_v2.plugins_runtime"] = ns

        # Import the plugin module
        module_file = manifest.get("module", "client.py")
        module_path = plugin_dir / module_file
        if not module_path.exists():
            log.warning("plugin_module_missing", plugin=name, module=module_file)
            return None

        mod_name = f"{pkg_name}.{Path(module_file).stem}"
        spec = importlib.util.spec_from_file_location(mod_name, module_path)
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = pkg_name  # type: ignore[attr-defined]
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        # Set plugin context so _client() factories can call secret()
        token = set_plugin_context(ctx)
        try:
            tools = self._collect_tools(name, module, ctx)
        finally:
            reset_plugin_context(token)

        description = manifest.get("description", "")
        plugin = LoadedPlugin(
            name=name,
            description=description,
            plugin_dir=plugin_dir,
            cli_module=manifest.get("cli_module", "cli.py"),
            scripts=manifest.get("scripts", {}),
            ctx=ctx,
            tools=tools,
        )
        log.info(
            "plugin_loaded",
            plugin=name,
            tools=[t.tool_name for t in tools],
        )
        return plugin

    def _resolve_plugin_for_cli(self, tool: str) -> LoadedPlugin | None:
        plugin = self.plugins.get(tool)
        if plugin:
            return plugin

        # Allow script aliases from [project.scripts] to map back to plugins.
        for candidate in self.plugins.values():
            if tool in candidate.scripts:
                return candidate
        return None

    def list_cli_tools(self) -> dict[str, dict[str, Any]]:
        """Return dynamic CLI tool metadata for all loaded plugins."""
        cli_tools: dict[str, dict[str, Any]] = {}
        for plugin in self.plugins.values():
            if not plugin.cli_path.exists():
                continue
            aliases = sorted(plugin.scripts.keys())
            cli_tools[plugin.name] = {
                "plugin": plugin.name,
                "description": plugin.description,
                "cli_path": str(plugin.cli_path),
                "tool_count": len(plugin.tools),
                "aliases": aliases,
            }
            for alias in aliases:
                cli_tools[alias] = {
                    "plugin": plugin.name,
                    "description": plugin.description,
                    "cli_path": str(plugin.cli_path),
                    "tool_count": len(plugin.tools),
                    "aliases": aliases,
                }
        return cli_tools

    def run_cli(self, tool: str, args: list[str]) -> str:
        """Run a plugin CLI dynamically without static allowlists."""
        plugin = self._resolve_plugin_for_cli(tool)
        if plugin is None:
            available = sorted(self.list_cli_tools().keys())
            return json.dumps(
                {
                    "error": f"Unknown CLI tool '{tool}'",
                    "available": available,
                }
            )

        cli_path = plugin.cli_path
        if not cli_path.exists():
            return json.dumps(
                {
                    "error": f"CLI not found for plugin '{plugin.name}'",
                    "expected_path": str(cli_path),
                }
            )

        cli_module_name = f"ai_v2.plugins_runtime.{plugin.name}.{cli_path.stem}"
        cli_spec = importlib.util.spec_from_file_location(cli_module_name, cli_path)
        if not cli_spec or not cli_spec.loader:
            return json.dumps(
                {
                    "error": f"Unable to load CLI module for plugin '{plugin.name}'",
                    "cli_path": str(cli_path),
                }
            )

        cli_module = importlib.util.module_from_spec(cli_spec)
        cli_module.__package__ = f"ai_v2.plugins_runtime.{plugin.name}"  # type: ignore[attr-defined]
        sys.modules[cli_module_name] = cli_module

        original_env: dict[str, str | None] = {}
        for key, value in plugin.ctx.secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            cli_spec.loader.exec_module(cli_module)
            app = getattr(cli_module, "app", None)
            if app is None:
                return json.dumps(
                    {
                        "error": f"CLI app not found for plugin '{plugin.name}'",
                        "expected_object": "app",
                    }
                )

            if hasattr(app, "registered_commands"):
                from typer.main import get_command

                app = get_command(app)

            runner = CliRunner()
            result = runner.invoke(app, args, prog_name=plugin.name)
            output = (result.output or "").strip()
            if result.exit_code != 0:
                details: dict[str, Any] = {
                    "error": f"CLI failed for plugin '{plugin.name}'",
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
                    "error": f"CLI raised for plugin '{plugin.name}'",
                    "detail": str(exc),
                }
            )
        finally:
            for key, previous in original_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def plugin_test_matrix(self) -> list[dict[str, Any]]:
        """Summarize import/discovery/CLI readiness for loaded plugins."""
        matrix: list[dict[str, Any]] = []
        for plugin in sorted(self.plugins.values(), key=lambda p: p.name):
            matrix.append(
                {
                    "plugin": plugin.name,
                    "library_import": True,
                    "discovered_tools": [tool.tool_name for tool in plugin.tools],
                    "cli_available": plugin.cli_path.exists(),
                    "cli_path": str(plugin.cli_path),
                    "aliases": sorted(plugin.scripts.keys()),
                }
            )
        return matrix

    def smoke_test_registry(self) -> list[dict[str, Any]]:
        """Verify registry integrity for plugins, tools, and CLI aliases."""
        entries = self.list_cli_tools()
        results: list[dict[str, Any]] = []

        for plugin in sorted(self.plugins.values(), key=lambda p: p.name):
            problems: list[str] = []
            if not plugin.tools:
                problems.append("no_discovered_tools")
            if plugin.cli_path.exists() and plugin.name not in entries:
                problems.append("plugin_missing_from_cli_registry")
            for alias in plugin.scripts:
                if alias not in entries:
                    problems.append(f"missing_alias:{alias}")

            results.append(
                {
                    "plugin": plugin.name,
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
        """Run a CLI smoke test for each loaded plugin that has a cli.py."""
        args = cli_args or ["--help"]
        results: list[dict[str, Any]] = []
        for plugin in sorted(self.plugins.values(), key=lambda p: p.name):
            if not plugin.cli_path.exists():
                results.append(
                    {
                        "plugin": plugin.name,
                        "status": "missing_cli",
                        "cli_path": str(plugin.cli_path),
                    }
                )
                continue

            output = self.run_cli(plugin.name, args)
            parsed = self._parse_cli_output(output)
            if parsed is not None:
                results.append(
                    {
                        "plugin": plugin.name,
                        "status": "failed",
                        "details": parsed,
                    }
                )
                continue

            results.append(
                {
                    "plugin": plugin.name,
                    "status": "ok",
                    "cli_path": str(plugin.cli_path),
                }
            )
        return results

    def smoke_test_aliases(self, cli_args: list[str] | None = None) -> list[dict[str, Any]]:
        """Run CLI smoke tests via script aliases from plugin manifests."""
        args = cli_args or ["--help"]
        results: list[dict[str, Any]] = []

        for plugin in sorted(self.plugins.values(), key=lambda p: p.name):
            aliases = sorted(plugin.scripts)
            if not aliases:
                results.append({"plugin": plugin.name, "status": "missing_aliases"})
                continue

            for alias in aliases:
                output = self.run_cli(alias, args)
                parsed = self._parse_cli_output(output)
                if parsed is not None:
                    results.append(
                        {
                            "plugin": plugin.name,
                            "alias": alias,
                            "status": "failed",
                            "details": parsed,
                        }
                    )
                    continue

                results.append(
                    {
                        "plugin": plugin.name,
                        "alias": alias,
                        "status": "ok",
                    }
                )

        return results

    @staticmethod
    def _collect_tools(plugin_name: str, module: Any, ctx: PluginContext) -> list[LoadedTool]:
        """Collect tools from a plugin module.

        The module must have a _client() factory. Call it once to get a cached
        instance and expose every public method as a tool.
        """
        tools: list[LoadedTool] = []
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
                tools.append(LoadedTool(plugin_name, method_name, method, ctx))
                seen.add(method_name)

        return tools

    def register_mcp_tools(self, mcp: Any) -> int:
        """Register all loaded plugin tools as MCP tools."""
        count = 0
        for plugin in self.plugins.values():
            for tool in plugin.tools:
                _register_mcp_tool(mcp, tool)
                count += 1
        log.info("mcp_plugin_tools_registered", count=count)
        return count

    def list_mcp_tools(self) -> list[dict[str, str]]:
        """Return all loaded plugin tools that are exposed to MCP."""
        items: list[dict[str, str]] = []
        for plugin in sorted(self.plugins.values(), key=lambda p: p.name):
            for tool in sorted(plugin.tools, key=lambda t: t.tool_name):
                items.append(
                    {
                        "name": tool.qualified_name,
                        "plugin": plugin.name,
                        "tool": tool.tool_name,
                        "description": tool.fn.__doc__ or "",
                    }
                )
        return items

    def create_rest_router(self) -> APIRouter:
        """Create a FastAPI router with all plugin tools as POST endpoints."""
        from .deps import verify_api_key

        router = APIRouter(
            prefix="/plugins",
            dependencies=[__import__("fastapi").Depends(verify_api_key)],
        )

        for plugin in self.plugins.values():
            for tool in plugin.tools:
                _register_rest_endpoint(router, tool)

        # List endpoint
        @router.get("")
        async def list_plugins() -> dict:
            return {
                name: {
                    "description": p.description,
                    "tools": [t.tool_name for t in p.tools],
                }
                for name, p in self.plugins.items()
            }

        return router


def _make_wrapper(tool: LoadedTool) -> Callable:
    """Wrap a plugin tool function to inject context and handle errors."""

    async def wrapper(**kwargs: Any) -> str:
        token = set_plugin_context(tool.ctx)
        try:
            if inspect.iscoroutinefunction(tool.fn):
                result = await tool.fn(**kwargs)
            else:
                result = tool.fn(**kwargs)
            if isinstance(result, str):
                return result
            return json.dumps(result, default=str)
        except Exception as e:
            return json.dumps(
                {
                    "error": str(e),
                    "plugin": tool.plugin_name,
                    "tool": tool.tool_name,
                }
            )
        finally:
            reset_plugin_context(token)

    # Preserve original signature for schema generation
    wrapper.__name__ = tool.qualified_name.replace(".", "_")
    wrapper.__doc__ = tool.fn.__doc__ or f"{tool.plugin_name} — {tool.tool_name}"
    wrapper.__signature__ = inspect.signature(tool.fn)  # type: ignore[attr-defined]
    try:
        wrapper.__annotations__ = get_type_hints(tool.fn)
    except Exception:
        wrapper.__annotations__ = getattr(tool.fn, "__annotations__", {})
    return wrapper


def _register_mcp_tool(mcp: Any, tool: LoadedTool) -> None:
    """Register a single plugin tool as an MCP tool."""
    wrapper = _make_wrapper(tool)
    # FastMCP uses the function name as the tool name
    wrapper.__name__ = tool.qualified_name.replace(".", "_")
    mcp.tool(name=tool.qualified_name)(wrapper)


def _register_rest_endpoint(router: APIRouter, tool: LoadedTool) -> None:
    """Register a single plugin tool as a REST POST endpoint."""
    sig = inspect.signature(tool.fn)
    try:
        hints = get_type_hints(tool.fn)
    except Exception:
        hints = getattr(tool.fn, "__annotations__", {})

    # Build Pydantic model from function signature
    fields: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        param_type = hints.get(param_name, Any)
        if param.default is inspect.Parameter.empty:
            fields[param_name] = (param_type, ...)
        else:
            fields[param_name] = (param_type, param.default)

    model_name = f"{tool.plugin_name}_{tool.tool_name}_Input"
    InputModel = create_model(model_name, **fields)

    wrapper = _make_wrapper(tool)

    async def endpoint(body: InputModel) -> dict:  # type: ignore[valid-type]
        result = await wrapper(**body.model_dump())
        return {"plugin": tool.plugin_name, "tool": tool.tool_name, "result": result}

    endpoint.__name__ = f"{tool.plugin_name}_{tool.tool_name}"
    router.post(
        f"/{tool.plugin_name}/{tool.tool_name}",
        name=tool.qualified_name,
        summary=tool.fn.__doc__ or tool.tool_name,
    )(endpoint)
