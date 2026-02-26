FROM ubuntu:24.04

# ── System packages ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core
    git curl wget ca-certificates gnupg lsb-release sudo \
    # Build tools
    build-essential pkg-config cmake ninja-build \
    libssl-dev libclang-dev protobuf-compiler \
    # Utilities
    ripgrep fd-find jq tree tmux unzip xz-utils gettext-base \
    # Python
    python3 python3-pip python3-venv python3-dev \
    # Process management
    procps htop \
    && rm -rf /var/lib/apt/lists/* \
    # fd-find installs as fdfind — symlink to fd
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd

# ── GitHub CLI ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Rust ─────────────────────────────────────────────────────────────────────
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# ── uv (Python package manager) ─────────────────────────────────────────────
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# ── Foundry (forge, cast, anvil, chisel) ─────────────────────────────────────
RUN curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup \
    && cp /root/.foundry/bin/* /usr/local/bin/

# ── Harness CLIs (amp, claude-code, codex) ───────────────────────────────────
RUN curl -fsSL https://ampcode.com/install.sh | bash \
    && cp -L /root/.amp/bin/amp /usr/local/bin/amp \
    && chmod a+rx /usr/local/bin/amp
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# ── Non-root agent user ─────────────────────────────────────────────────────
# claude-code requires non-root for --dangerously-skip-permissions
RUN useradd -m -s /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/agent

# ── Set up agent user environment ────────────────────────────────────────────
USER agent
WORKDIR /home/agent

# Rust toolchain for agent user
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/home/agent/.cargo/bin:${PATH}"

# uv for agent user
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/agent/.local/bin:${PATH}"

# Foundry for agent user
RUN curl -L https://foundry.paradigm.xyz | bash \
    && /home/agent/.foundry/bin/foundryup
ENV PATH="/home/agent/.foundry/bin:${PATH}"

# Amp runtime marker so it doesn't fall back to plain bun
RUN mkdir -p ~/.amp/bin ~/.config/amp ~/.codex \
    && ln -s /usr/local/bin/amp ~/.amp/bin/amp

# Git defaults
RUN git config --global init.defaultBranch main \
    && git config --global push.autoSetupRemote true \
    && git config --global --add safe.directory '*'

# ── Workspace ────────────────────────────────────────────────────────────────
RUN mkdir -p ~/github ~/workspace

# ── Bake config templates (envsubst'd at runtime) ───────────────────────────
COPY --chown=agent:agent config/amp-settings.json   /home/agent/.config/amp/settings.json.tmpl
COPY --chown=agent:agent config/claude-settings.json /home/agent/.claude.json.tmpl
COPY --chown=agent:agent config/codex-config.toml    /home/agent/.codex/config.toml.tmpl

# ── System prompt ────────────────────────────────────────────────────────────
COPY --chown=agent:agent SYSTEM_PROMPT.md /home/agent/AGENTS.md

# ── Entrypoint ───────────────────────────────────────────────────────────────
COPY --chmod=755 entrypoint.sh /entrypoint.sh
WORKDIR /home/agent/workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
