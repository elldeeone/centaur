from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_HOME = Path.home() / ".centaur"


@dataclass
class OnboardingState:
    org: str = ""
    assistant_name: str = "centaur"
    domain: str = ""
    admin_email: str = ""
    install_mode: str = "local"
    secret_backend: str = "local-env"
    overlay_path: str = ""
    completed_steps: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def mark_done(self, step: str) -> None:
        if step not in self.completed_steps:
            self.completed_steps.append(step)


def state_path(home: Path = DEFAULT_HOME) -> Path:
    return home / "onboarding-state.json"


def config_path(home: Path = DEFAULT_HOME) -> Path:
    return home / "config.json"


def load_state(home: Path = DEFAULT_HOME) -> OnboardingState:
    path = state_path(home)
    if not path.exists():
        return OnboardingState()
    raw = json.loads(path.read_text())
    return OnboardingState(**raw)


def save_state(state: OnboardingState, home: Path = DEFAULT_HOME) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = state_path(home)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")
    config_path(home).write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")
    (home / "logs").mkdir(exist_ok=True)
    return path
