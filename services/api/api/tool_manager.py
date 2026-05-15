"""Tool discovery, loading, and registration."""

from __future__ import annotations

import asyncio
import base64
import difflib
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
import tomllib
import types
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from toon_format import encode as toon_encode

from api.api_keys import check_scope
from api.laminar_tracing import set_span_attributes, start_span
from api.vm_metrics import record_tool_call
from api.deps import get_key_info, get_sandbox_claims, verify_api_key
from centaur_sdk import ToolContext, reset_tool_context, set_tool_context

log = structlog.get_logger()


@dataclass(frozen=True)
class HeaderSecret:
    """Header-based HTTP credential injected by iron-proxy's ``secrets`` transform.

    The tool sees ``replacer`` (a placeholder token) inside the sandbox; iron-proxy
    swaps it for the real value resolved from ``secret_ref`` (env var or 1Password
    item) when scanning outbound request headers.
    """

    name: str
    secret_ref: str
    replacer: str


@dataclass(frozen=True)
class GcpAuthSecret:
    """GCP service-account keyfile fed to iron-proxy's ``gcp_auth`` transform.

    iron-proxy loads the keyfile from ``secret_ref``, mints OAuth2 tokens, and
    injects them as ``Authorization: Bearer`` on ``*.googleapis.com``.
    """

    name: str
    secret_ref: str


@dataclass(frozen=True)
class PgDsnSecret:
    """Postgres DSN proxied by iron-proxy's ``postgres`` transform.

    The sandbox sees a local DSN pointing at iron-proxy on a per-secret listen
    port; iron-proxy fronts the real upstream resolved from ``secret_ref``.

    ``database`` is the dbname the sandbox connects to. iron-proxy forwards
    the client's startup-packet database to the upstream, so this must match
    the dbname in the upstream DSN for the connection to land on the right
    database without an explicit ``\\c`` after connecting.
    """

    name: str
    secret_ref: str
    database: str


SecretDef = HeaderSecret | GcpAuthSecret | PgDsnSecret


def _parse_secret(entry: Any) -> SecretDef:
    """Normalize a single secret entry from pyproject.toml into a SecretDef.

    Raw strings are accepted for back-compat: ``"FOO"`` becomes
    ``HeaderSecret(name="FOO", secret_ref="FOO", replacer="FOO")``.
    """
    if isinstance(entry, str):
        return HeaderSecret(name=entry, secret_ref=entry, replacer=entry)
    if not isinstance(entry, dict):
        raise ValueError(f"secret entry must be a string or table, got {type(entry).__name__}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"secret entry missing 'name': {entry!r}")
    secret_type = entry.get("type", "header")
    secret_ref = entry.get("secret_ref", name)
    if not isinstance(secret_ref, str) or not secret_ref:
        raise ValueError(f"secret entry has invalid 'secret_ref': {entry!r}")
    if secret_type == "header":
        replacer = entry.get("replacer", name)
        if not isinstance(replacer, str) or not replacer:
            raise ValueError(f"secret entry has invalid 'replacer': {entry!r}")
        return HeaderSecret(name=name, secret_ref=secret_ref, replacer=replacer)
    if secret_type == "gcp_auth":
        return GcpAuthSecret(name=name, secret_ref=secret_ref)
    if secret_type == "pg_dsn":
        database = entry.get("database")
        if not isinstance(database, str) or not database:
            raise ValueError(
                f"pg_dsn entry {name!r} requires a non-empty 'database' field"
            )
        return PgDsnSecret(name=name, secret_ref=secret_ref, database=database)
    raise ValueError(f"unknown secret type {secret_type!r}")


def _parse_secrets(entries: Any) -> list[SecretDef]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError("'secrets'/'optional_secrets' must be an array")
    return [_parse_secret(e) for e in entries]


async def _resolve_secrets(secrets: list[SecretDef]) -> dict[str, str]:
    """Return placeholder values for header secrets.

    Only ``HeaderSecret`` entries end up in the tool's ``ToolContext`` — the
    tool gets back the ``replacer`` token, which iron-proxy swaps for the
    real credential at the network boundary. ``GcpAuthSecret`` and
    ``PgDsnSecret`` are not exposed via context; they reach the tool through
    environment variables set on the sandbox by the kubernetes backend.
    """
    return {s.name: s.replacer for s in secrets if isinstance(s, HeaderSecret)}


_MAX_INLINE_TOOL_BINARY_BYTES = max(
    1024, int(os.getenv("TOOL_BINARY_INLINE_MAX_BYTES", str(1 * 1024 * 1024)))
)
_TOOL_BINARY_PREVIEW_BYTES = max(
    128, int(os.getenv("TOOL_BINARY_PREVIEW_BYTES", str(32 * 1024)))
)

# Threshold for extracting base64-encoded file data from tool results into
# the attachments table.  Anything larger gets stored as an attachment and
# replaced with a download URL so it doesn't bloat the agent context window.
_ATTACHMENT_EXTRACT_MIN_BYTES = 64 * 1024  # 64 KB

# Maximum wall-clock seconds a single tool call may run before being cancelled.
_TOOL_CALL_TIMEOUT_S = int(os.getenv("TOOL_CALL_TIMEOUT_S", "120"))


async def _extract_tool_attachment(
    result: dict[str, Any],
    *,
    request: Request | None,
    thread_key: str | None,
    tool_name: str,
) -> dict[str, Any]:
    """If *result* contains a large base64 ``data`` field, store it as an
    attachment and replace the field with a download URL.

    Returns the (possibly modified) result dict.
    """
    data_b64 = result.get("data")
    if not isinstance(data_b64, str) or len(data_b64) < _ATTACHMENT_EXTRACT_MIN_BYTES:
        return result

    # Heuristic: looks like base64 (only base64 chars, length divisible by 4)
    if not re.fullmatch(r"[A-Za-z0-9+/=\n\r]+", data_b64[:256]):
        return result

    pool = getattr(getattr(request, "app", None), "state", None)
    pool = getattr(pool, "db_pool", None) if pool else None
    if pool is None:
        return result

    try:
        raw_bytes = base64.b64decode(data_b64)
    except Exception:
        return result

    att_id = f"att-{uuid.uuid4().hex[:16]}"
    mime_type = result.get("mime_type", "application/octet-stream")
    filename = result.get("filename") or f"{tool_name}_output"

    await pool.execute(
        "INSERT INTO attachments (id, thread_key, message_id, name, mime_type, data) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO NOTHING",
        att_id,
        thread_key or "",
        None,
        filename,
        mime_type,
        raw_bytes,
    )
    log.info(
        "tool_result_attachment_stored",
        tool=tool_name,
        attachment_id=att_id,
        filename=filename,
        mime_type=mime_type,
        size=len(raw_bytes),
    )

    out = {k: v for k, v in result.items() if k != "data"}
    out["attachment_id"] = att_id
    out["download_url"] = f"/agent/attachments/{att_id}/download"
    return out


class ToolMethod:
    def __init__(self, method_name: str, fn: Callable):
        self.method_name = method_name
        self.fn = fn


_LIFECYCLE_METHODS = frozenset({"close", "connect", "disconnect", "shutdown"})

_COMMON_ARGUMENT_ALIASES: dict[str, str] = {
    "channel_id": "channel",
    "count": "limit",
    "max_results": "limit",
    "page_size": "limit",
    "range": "range_notation",
    "sql": "query",
    "table": "table_name",
}


def _tool_arg_validation_error(
    method: ToolMethod, args: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a structured argument error before invoking a tool method."""
    sig = inspect.signature(method.fn)
    params = sig.parameters
    accepts_var_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    valid_names = {
        name
        for name, param in params.items()
        if param.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    }
    if not accepts_var_kwargs:
        unexpected = sorted(set(args) - valid_names)
        if unexpected:
            suggestions = {
                key: (
                    _COMMON_ARGUMENT_ALIASES.get(key)
                    if _COMMON_ARGUMENT_ALIASES.get(key) in valid_names
                    else (difflib.get_close_matches(key, valid_names, n=1) or [None])[0]
                )
                for key in unexpected
            }
            return {
                "error": "tool_argument_validation_failed",
                "message": f"Unexpected argument(s): {', '.join(unexpected)}",
                "unexpected_args": unexpected,
                "accepted_args": sorted(valid_names),
                "did_you_mean": {k: v for k, v in suggestions.items() if v},
            }

    missing = sorted(
        name
        for name, param in params.items()
        if param.default is inspect.Parameter.empty
        and param.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        and name not in args
    )
    if missing:
        return {
            "error": "tool_argument_validation_failed",
            "message": f"Missing required argument(s): {', '.join(missing)}",
            "missing_args": missing,
            "accepted_args": sorted(valid_names),
        }
    return None


def _normalize_for_serialization(data: Any) -> Any:
    """Normalize rich Python values into JSON-friendly structures."""
    if data is None or isinstance(data, (str, int, float, bool)):
        return data
    if isinstance(data, bytes):
        if len(data) > _MAX_INLINE_TOOL_BINARY_BYTES:
            return {
                "encoding": "base64_preview",
                "byte_length": len(data),
                "content_base64": base64.b64encode(
                    data[:_TOOL_BINARY_PREVIEW_BYTES]
                ).decode(),
            }
        return {
            "encoding": "base64",
            "byte_length": len(data),
            "content_base64": base64.b64encode(data).decode(),
        }
    if isinstance(data, Enum):
        return data.value
    if is_dataclass(data):
        return _normalize_for_serialization(asdict(data))
    if isinstance(data, dict):
        return {
            str(key): _normalize_for_serialization(value) for key, value in data.items()
        }
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
        toon = toon_encode(normalized)
        compact_json = json.dumps(normalized, separators=(",", ":"), default=str)
        return toon if len(toon) <= len(compact_json) else compact_json
    except Exception:
        return json.dumps(normalized, default=str)


def _payload_size_bytes(value: Any) -> int:
    normalized = _normalize_for_serialization(value)
    try:
        return len(
            json.dumps(normalized, separators=(",", ":"), default=str).encode("utf-8")
        )
    except Exception:
        return len(str(normalized).encode("utf-8", errors="replace"))


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
    # typing.Optional / Union / str | int (PEP 604)
    if (
        isinstance(annotation, types.UnionType)
        or (origin is not None and str(origin) == "typing.Union")
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
        ctx: ToolContext,
        methods: list[ToolMethod],
        hosts: list[str] | None = None,
        secrets: list[SecretDef] | None = None,
        optional_secrets: list[SecretDef] | None = None,
    ):
        self.name = name
        self.description = description
        self.ctx = ctx
        self.methods = methods
        self.hosts: list[str] = hosts or []
        self.secrets: list[SecretDef] = secrets or []
        self.optional_secrets: list[SecretDef] = optional_secrets or []

    @property
    def all_secrets(self) -> list[SecretDef]:
        return self.secrets + self.optional_secrets

    @property
    def secret_names(self) -> list[str]:
        """Names of all declared secrets (required + optional), in declaration order."""
        return [s.name for s in self.all_secrets]


@dataclass
class LoadedPersona:
    name: str
    description: str
    engine: str
    default_repo: str | None
    prompt_content: str
    prompt_file: str
    has_custom_executor: bool  # True if run.py exists in the persona dir
    tool_dir: Path


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
        self.tools: dict[str, LoadedTool] = {}
        self.personas: dict[str, LoadedPersona] = {}
        self.load_failures: list[dict[str, str]] = []
        self._reload_lock = threading.Lock()

    def _collect_tools(self) -> list[tuple[Path, dict]]:
        """Read pyproject.toml from each tool dir.

        Directories in ``self.tools_dirs`` are scanned in order.  When the same
        tool name appears in a later directory it shadows the earlier one (useful
        for private-overrides-public).

        Supports one level of category subdirectories: if a child directory has
        no ``pyproject.toml`` it is treated as a category folder and its children
        are scanned for tools (e.g. ``tools/crypto/alchemy/``).
        """
        seen: dict[str, int] = {}
        tools: list[tuple[Path, dict]] = []
        for dir_idx, base_dir in enumerate(self.tools_dirs):
            if not base_dir.exists():
                continue
            # Collect candidate tool dirs, expanding category subdirectories
            candidates: list[Path] = []
            for child in sorted(base_dir.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                if (child / "pyproject.toml").exists():
                    candidates.append(child)
                else:
                    # Category dir — scan its children
                    for sub in sorted(child.iterdir()):
                        if sub.is_dir() and not sub.name.startswith((".", "_")):
                            candidates.append(sub)

            for tool_dir in candidates:
                pyproject_path = tool_dir / "pyproject.toml"
                if not pyproject_path.exists():
                    continue

                with open(pyproject_path, "rb") as f:
                    pyproject = tomllib.load(f)

                project = pyproject.get("project", {})
                tool_conf = pyproject.get("tool", {}).get("ai-v2", {})

                name = tool_dir.name
                hosts = tool_conf.get("hosts", [])
                try:
                    secrets = _parse_secrets(tool_conf.get("secrets"))
                    optional_secrets = _parse_secrets(tool_conf.get("optional_secrets"))
                except ValueError as exc:
                    log.warning(
                        "tool_invalid_secrets",
                        tool=name,
                        error=str(exc),
                    )
                    continue

                # Validate host patterns
                for h in hosts:
                    if h in ("*", "*.com", "*.org", "*.net", "*.io"):
                        log.warning(
                            "tool_invalid_host",
                            tool=name,
                            host=h,
                            reason="catch-all domain not allowed",
                        )
                    elif re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
                        log.warning(
                            "tool_invalid_host",
                            tool=name,
                            host=h,
                            reason="IP addresses not allowed",
                        )

                # Skip persona entries — they are loaded separately
                if tool_conf.get("type") == "persona":
                    continue

                meta = {
                    "name": name,
                    "description": project.get("description", ""),
                    "module": tool_conf.get("module", "client.py"),
                    "hosts": hosts,
                    "secrets": secrets,
                    "optional_secrets": optional_secrets,
                }

                if name in seen:
                    prev_idx = seen[name]
                    prev_pos = next(
                        i for i, (_, m) in enumerate(tools) if m["name"] == name
                    )
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

    def _collect_personas(self) -> list[tuple[Path, dict, dict]]:
        """Scan tools dirs for persona entries (type=persona in pyproject.toml)."""
        personas: list[tuple[Path, dict, dict]] = []
        for base_dir in self.tools_dirs:
            if not base_dir.exists():
                continue
            for child in sorted(base_dir.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                # Check direct children and category subdirectories
                candidates: list[Path] = []
                if (child / "pyproject.toml").exists():
                    candidates.append(child)
                else:
                    for sub in sorted(child.iterdir()):
                        if sub.is_dir() and not sub.name.startswith((".", "_")):
                            if (sub / "pyproject.toml").exists():
                                candidates.append(sub)

                for tool_dir in candidates:
                    with open(tool_dir / "pyproject.toml", "rb") as f:
                        pyproject = tomllib.load(f)
                    project = pyproject.get("project", {})
                    tool_conf = pyproject.get("tool", {}).get("ai-v2", {})
                    if tool_conf.get("type") != "persona":
                        continue
                    personas.append((tool_dir, project, tool_conf))
        return personas

    def _load_persona(
        self, tool_dir: Path, project: dict, tool_conf: dict
    ) -> LoadedPersona:
        """Load a single persona from its directory."""
        name = tool_dir.name
        prompt_file = tool_conf.get("prompt", "PROMPT.md")
        prompt_path = tool_dir / prompt_file
        prompt_content = prompt_path.read_text() if prompt_path.exists() else ""
        has_custom_executor = (tool_dir / "run.py").exists()
        return LoadedPersona(
            name=name,
            description=project.get("description", ""),
            engine=tool_conf.get("engine", "amp"),
            default_repo=tool_conf.get("default_repo"),
            prompt_content=prompt_content,
            prompt_file=prompt_file,
            has_custom_executor=has_custom_executor,
            tool_dir=tool_dir,
        )

    def get_persona(self, name: str) -> LoadedPersona | None:
        """Return a loaded persona by name, or None."""
        return self.personas.get(name)

    def discover(self) -> list[LoadedTool]:
        """Discover and load all tools and personas."""
        existing = [d for d in self.tools_dirs if d.exists()]
        if not existing:
            self.load_failures = []
            log.info("tools_dirs_missing", paths=[str(d) for d in self.tools_dirs])
            return []

        tool_entries = self._collect_tools()

        # Load each tool
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

        # Load personas
        personas: dict[str, LoadedPersona] = {}
        for tool_dir, project, tool_conf in self._collect_personas():
            try:
                persona = self._load_persona(tool_dir, project, tool_conf)
                personas[persona.name] = persona
                log.info("persona_loaded", persona=persona.name, engine=persona.engine)
            except Exception as exc:
                log.warning(
                    "persona_load_failed", persona=tool_dir.name, error=str(exc)
                )
        self.personas = personas

        log.info(
            "tools_discovery_complete",
            loaded=len(loaded),
            failed=len(load_failures),
            failed_tools=[f["name"] for f in load_failures],
            personas=list(personas.keys()),
        )
        return loaded

    # Hardcoded infrastructure entries for the injection map. Each entry is a
    # ``HeaderSecret`` paired with the hosts iron-proxy attaches it to.
    _INFRA_SECRETS: ClassVar[list[tuple[HeaderSecret, tuple[str, ...]]]] = [
        (HeaderSecret("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"), ("api.anthropic.com",)),
        (HeaderSecret("OPENAI_API_KEY", "OPENAI_API_KEY", "OPENAI_API_KEY"), ("api.openai.com",)),
        (HeaderSecret("XAI_API_KEY", "XAI_API_KEY", "XAI_API_KEY"), ("api.x.ai",)),
        (HeaderSecret("GEMINI_API_KEY", "GEMINI_API_KEY", "GEMINI_API_KEY"), ("generativelanguage.googleapis.com",)),
        (HeaderSecret("AMP_API_KEY", "AMP_API_KEY", "AMP_API_KEY"), ("ampcode.com",)),
        (HeaderSecret("GITHUB_TOKEN", "GITHUB_TOKEN", "GITHUB_TOKEN"), ("github.com", "api.github.com")),
        (HeaderSecret("SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN"), ("*.slack.com",)),
    ]

    def collect_secrets(self) -> list[tuple[SecretDef, tuple[str, ...]]]:
        """Return all secrets (infra + tool) paired with the hosts they apply to.

        Hosts apply to ``HeaderSecret`` only — ``GcpAuthSecret`` and
        ``PgDsnSecret`` ignore the host list (gcp_auth is fixed to
        ``*.googleapis.com``; pg_dsn is a TCP listener, no host).
        """
        out: list[tuple[SecretDef, tuple[str, ...]]] = [
            (s, hosts) for s, hosts in self._INFRA_SECRETS
        ]
        for lt in self.tools.values():
            hosts = tuple(lt.hosts)
            for s in lt.all_secrets:
                out.append((s, hosts))
        return out

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
        ctx = ToolContext(name=name, secrets={})

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

        # Ensure parent namespaces exist
        if "shared" not in sys.modules:
            ns = types.ModuleType("shared")
            ns.__path__ = []  # type: ignore[attr-defined]
            sys.modules["shared"] = ns
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

        # Set tool context so _client() factories can call secret()
        token = set_tool_context(ctx)
        try:
            spec.loader.exec_module(module)
            methods = self._collect_methods(module)
        finally:
            reset_tool_context(token)

        description = manifest.get("description", "")
        loaded_tool = LoadedTool(
            name=name,
            description=description,
            ctx=ctx,
            methods=methods,
            hosts=manifest.get("hosts", []),
            secrets=manifest.get("secrets", []),
            optional_secrets=manifest.get("optional_secrets", []),
        )
        log.info(
            "tool_loaded",
            tool=name,
            methods=[m.method_name for m in methods],
        )
        return loaded_tool

    @staticmethod
    def _collect_methods(module: Any) -> list[ToolMethod]:
        """Collect tools from a tool module.

        The module must have a _client() factory. Call it once to get a cached
        instance and expose every public method as a tool.
        """
        methods: list[ToolMethod] = []

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
                methods.append(ToolMethod(method_name, method))

        return methods

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

    async def call_tool_raw(
        self,
        tool_name: str,
        method_name: str,
        args: dict[str, Any],
        *,
        request: Request | None = None,
    ) -> Any:
        """Call a tool method by name and return the raw Python result.

        Like ``call_tool`` but skips TOON/JSON serialization so the caller gets
        the native return value (e.g. a dict with binary data).
        """
        lt = self.tools.get(tool_name)
        if not lt:
            return {
                "error": f"Tool '{tool_name}' not found",
                "available": sorted(self.tools.keys()),
            }

        method = next((m for m in lt.methods if m.method_name == method_name), None)
        if not method:
            return {
                "error": f"Method '{method_name}' not found in tool '{tool_name}'",
                "available_methods": sorted(m.method_name for m in lt.methods),
            }

        sandbox_claims = get_sandbox_claims(request) if request is not None else None
        call_fields = {
            "tool_name": tool_name,
            "tool_method": method_name,
            "arg_keys": sorted(args.keys()),
            "arg_size_bytes": _payload_size_bytes(args),
            **(
                {
                    "thread_key": sandbox_claims.get("thread_key"),
                    "sandbox_container_id": sandbox_claims.get("container_id"),
                }
                if sandbox_claims
                else {}
            ),
        }
        t0 = time.monotonic()
        log.info("tool_call_started", **call_fields)
        validation_error = _tool_arg_validation_error(method, args)
        if validation_error is not None:
            log.warning(
                "tool_argument_validation_failed",
                error=validation_error["message"],
                **call_fields,
            )
            return validation_error

        ctx = lt.ctx
        all_secrets = lt.all_secrets
        if all_secrets:
            resolved = await _resolve_secrets(all_secrets)
            if resolved:
                ctx = ToolContext(
                    name=lt.name,
                    secrets={**lt.ctx.secrets, **resolved},
                    thread_key=sandbox_claims.get("thread_key")
                    if sandbox_claims
                    else None,
                    container_id=sandbox_claims.get("container_id")
                    if sandbox_claims
                    else None,
                )
            elif sandbox_claims:
                ctx = ToolContext(
                    name=lt.name,
                    secrets=dict(lt.ctx.secrets),
                    thread_key=sandbox_claims.get("thread_key"),
                    container_id=sandbox_claims.get("container_id"),
                )
        elif sandbox_claims:
            ctx = ToolContext(
                name=lt.name,
                secrets=dict(lt.ctx.secrets),
                thread_key=sandbox_claims.get("thread_key"),
                container_id=sandbox_claims.get("container_id"),
            )

        token = set_tool_context(ctx)
        try:
            with start_span(
                name="centaur.tool.call",
                span_type="TOOL",
                metadata={
                    "service": "api",
                    "tool_name": tool_name,
                    "tool_method": method_name,
                    **(
                        {"thread_key": sandbox_claims.get("thread_key")}
                        if sandbox_claims
                        else {}
                    ),
                },
            ):
                set_span_attributes(
                    {
                        "centaur.tool.name": tool_name,
                        "centaur.tool.method": method_name,
                        "centaur.tool.arg_keys": ",".join(sorted(args.keys())),
                        **(
                            {"centaur.thread_key": sandbox_claims.get("thread_key")}
                            if sandbox_claims
                            else {}
                        ),
                    }
                )
                if inspect.iscoroutinefunction(method.fn):
                    coro = method.fn(**args)
                else:
                    coro = asyncio.to_thread(method.fn, **args)
                result = await asyncio.wait_for(coro, timeout=_TOOL_CALL_TIMEOUT_S)
            duration_ms = round((time.monotonic() - t0) * 1000)
            log.info(
                "tool_call_completed",
                duration_ms=duration_ms,
                success=True,
                result_size_bytes=_payload_size_bytes(result),
                **call_fields,
            )
            return result
        except (SystemExit, Exception) as e:
            duration_ms = round((time.monotonic() - t0) * 1000)
            if isinstance(e, asyncio.TimeoutError):
                error_msg = f"Tool call timed out after {_TOOL_CALL_TIMEOUT_S}s"
            elif isinstance(e, SystemExit):
                error_msg = f"sys.exit({e.code})"
            else:
                error_msg = str(e)
            log.warning(
                "tool_call_completed",
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
                error_type=type(e).__name__,
                **call_fields,
            )
            return {"error": error_msg, "tool": tool_name, "method": method_name}
        finally:
            reset_tool_context(token)

    async def call_tool(
        self,
        tool_name: str,
        method_name: str,
        args: dict[str, Any],
        *,
        request: Request | None = None,
        format: str = "json",
    ) -> str | Any:
        """Call a tool method by name.

        *format* controls the response serialization:
        - ``"toon"``  – token-efficient TOON string (used by sandbox agents).
        - ``"json"``  – return the normalised Python object as-is (default).
        """
        lt = self.tools.get(tool_name)
        if not lt:
            return json.dumps(
                {
                    "error": f"Tool '{tool_name}' not found",
                    "available": sorted(self.tools.keys()),
                }
            )

        method = next((m for m in lt.methods if m.method_name == method_name), None)
        if not method:
            return json.dumps(
                {
                    "error": f"Method '{method_name}' not found in tool '{tool_name}'",
                    "available_methods": sorted(m.method_name for m in lt.methods),
                }
            )

        sandbox_claims = get_sandbox_claims(request) if request is not None else None
        call_fields = {
            "tool_name": tool_name,
            "tool_method": method_name,
            "arg_keys": sorted(args.keys()),
            "arg_size_bytes": _payload_size_bytes(args),
            **(
                {
                    "thread_key": sandbox_claims.get("thread_key"),
                    "sandbox_container_id": sandbox_claims.get("container_id"),
                }
                if sandbox_claims
                else {}
            ),
        }
        t0 = time.monotonic()
        log.info("tool_call_started", **call_fields)
        validation_error = _tool_arg_validation_error(method, args)
        if validation_error is not None:
            log.warning(
                "tool_argument_validation_failed",
                error=validation_error["message"],
                **call_fields,
            )
            return json.dumps(validation_error)

        # Resolve placeholder secrets for tools that declare them. Required
        # secrets gate availability elsewhere; optional secrets should still be
        # present in ToolContext when declared so tool code can choose to use
        # them.
        ctx = lt.ctx
        all_secrets = lt.all_secrets
        if all_secrets:
            resolved = await _resolve_secrets(all_secrets)
            log.info(
                "tool_secrets_resolved",
                tool=tool_name,
                keys=list(resolved.keys()),
                declared=[s.name for s in all_secrets],
            )
            if resolved:
                ctx = ToolContext(
                    name=lt.name,
                    secrets={**lt.ctx.secrets, **resolved},
                    thread_key=sandbox_claims.get("thread_key")
                    if sandbox_claims
                    else None,
                    container_id=sandbox_claims.get("container_id")
                    if sandbox_claims
                    else None,
                )
            elif sandbox_claims:
                ctx = ToolContext(
                    name=lt.name,
                    secrets=dict(lt.ctx.secrets),
                    thread_key=sandbox_claims.get("thread_key"),
                    container_id=sandbox_claims.get("container_id"),
                )
        elif sandbox_claims:
            ctx = ToolContext(
                name=lt.name,
                secrets=dict(lt.ctx.secrets),
                thread_key=sandbox_claims.get("thread_key"),
                container_id=sandbox_claims.get("container_id"),
            )

        token = set_tool_context(ctx)
        try:
            with start_span(
                name="centaur.tool.call",
                span_type="TOOL",
                metadata={
                    "service": "api",
                    "tool_name": tool_name,
                    "tool_method": method_name,
                    **(
                        {"thread_key": sandbox_claims.get("thread_key")}
                        if sandbox_claims
                        else {}
                    ),
                },
            ):
                set_span_attributes(
                    {
                        "centaur.tool.name": tool_name,
                        "centaur.tool.method": method_name,
                        "centaur.tool.arg_keys": ",".join(sorted(args.keys())),
                        **(
                            {"centaur.thread_key": sandbox_claims.get("thread_key")}
                            if sandbox_claims
                            else {}
                        ),
                    }
                )
                if inspect.iscoroutinefunction(method.fn):
                    coro = method.fn(**args)
                else:
                    coro = asyncio.to_thread(method.fn, **args)
                result = await asyncio.wait_for(coro, timeout=_TOOL_CALL_TIMEOUT_S)
            duration_ms = round((time.monotonic() - t0) * 1000)
            log.info(
                "tool_call_completed",
                duration_ms=duration_ms,
                success=True,
                result_size_bytes=_payload_size_bytes(result),
                **call_fields,
            )
            record_tool_call(tool_name, method_name, True, duration_ms / 1000)
            if isinstance(result, dict):
                thread_key = (
                    sandbox_claims.get("thread_key") if sandbox_claims else None
                )
                result = await _extract_tool_attachment(
                    result,
                    request=request,
                    thread_key=thread_key,
                    tool_name=tool_name,
                )
            if format == "toon":
                return result if isinstance(result, str) else _to_toon(result)
            return _normalize_for_serialization(result)
        except (SystemExit, Exception) as e:
            duration_ms = round((time.monotonic() - t0) * 1000)
            if isinstance(e, asyncio.TimeoutError):
                error_msg = f"Tool call timed out after {_TOOL_CALL_TIMEOUT_S}s"
            elif isinstance(e, SystemExit):
                error_msg = f"sys.exit({e.code})"
            else:
                error_msg = str(e)
            log.warning(
                "tool_call_completed",
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
                error_type=type(e).__name__,
                **call_fields,
            )
            record_tool_call(tool_name, method_name, False, duration_ms / 1000)
            return json.dumps(
                {"error": error_msg, "tool": tool_name, "method": method_name}
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

        def _require_tool_scope(request: Request, tool_name: str) -> None:
            key_info = get_key_info(request)
            if not check_scope(key_info, "tools", tool_name):
                raise HTTPException(
                    status_code=403,
                    detail=f"API key does not have access to tool '{tool_name}'",
                )

        @router.get("")
        async def list_tools(request: Request) -> dict:
            key_info = get_key_info(request)
            result = {}
            for name, p in pm.tools.items():
                if not check_scope(key_info, "tools", name):
                    continue
                required_headers = [s for s in p.secrets if isinstance(s, HeaderSecret)]
                if required_headers:
                    resolved = await _resolve_secrets(required_headers)
                    if len(resolved) < len(required_headers):
                        continue
                result[name] = {
                    "description": p.description,
                    "methods": [m.method_name for m in p.methods],
                }
            return result

        # ── Persona endpoints (registered before catch-all /{tool_name}) ─────

        @router.get("/personas")
        async def list_personas() -> dict:
            return {
                name: {
                    "description": p.description,
                    "engine": p.engine,
                    "default_repo": p.default_repo,
                    "has_custom_executor": p.has_custom_executor,
                }
                for name, p in pm.personas.items()
            }

        @router.get("/personas/{name}")
        async def get_persona_detail(name: str) -> dict:
            p = pm.personas.get(name)
            if not p:
                raise HTTPException(
                    status_code=404, detail=f"Persona '{name}' not found"
                )
            return {
                "name": p.name,
                "description": p.description,
                "engine": p.engine,
                "default_repo": p.default_repo,
                "prompt_file": p.prompt_file,
                "has_custom_executor": p.has_custom_executor,
                "tool_dir": str(p.tool_dir),
            }

        @router.get("/personas/{name}/prompt")
        async def get_persona_prompt(name: str):
            p = pm.personas.get(name)
            if not p:
                raise HTTPException(
                    status_code=404, detail=f"Persona '{name}' not found"
                )
            return PlainTextResponse(p.prompt_content)

        # ── Tool endpoints ───────────────────────────────────────────────────

        @router.get("/{tool_name}")
        async def describe_tool(tool_name: str, request: Request) -> dict:
            _require_tool_scope(request, tool_name)
            p = pm.tools.get(tool_name)
            if p:
                required_headers = [s for s in p.secrets if isinstance(s, HeaderSecret)]
                if required_headers:
                    resolved = await _resolve_secrets(required_headers)
                    if len(resolved) < len(required_headers):
                        raise HTTPException(
                            status_code=404,
                            detail=f"Tool '{tool_name}' is not available (missing secrets)",
                        )
            return pm.describe_tool(tool_name)

        @router.post("/{tool_name}/{method_name}")
        async def call_tool(tool_name: str, method_name: str, request: Request):
            raw_body = await request.body()
            body: dict[str, Any] = {}
            if raw_body:
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError as exc:
                    raise HTTPException(
                        status_code=400, detail="Request body must be valid JSON"
                    ) from exc
                if not isinstance(body, dict):
                    raise HTTPException(
                        status_code=400, detail="Request body must be a JSON object"
                    )
            _require_tool_scope(request, tool_name)
            accept = request.headers.get("accept", "")
            want_toon = "text/plain" in accept
            fmt = "toon" if want_toon else "json"
            result = await pm.call_tool(
                tool_name, method_name, body, request=request, format=fmt
            )
            if want_toon:
                return PlainTextResponse(
                    result if isinstance(result, str) else _to_toon(result)
                )
            return {"tool": tool_name, "method": method_name, "result": result}

        return router
