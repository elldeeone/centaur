FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gnupg \
    ripgrep fd-find jq yq tree gettext-base \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSL -o /usr/local/bin/dbmate "https://github.com/amacneil/dbmate/releases/latest/download/dbmate-linux-${ARCH}" \
    && chmod +x /usr/local/bin/dbmate \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
       > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# 1. Install core + tool dependencies (changes rarely)
COPY services/api/pyproject.toml ./
COPY centaur_sdk/ centaur_sdk/
RUN sed -i 's|path = "../../centaur_sdk"|path = "centaur_sdk"|' pyproject.toml \
    && rm -f uv.lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=tools,target=/tmp/tools \
    python -c "\
import tomllib, pathlib; \
deps = set(); \
[deps.update(tomllib.load(open(p,'rb')).get('project',{}).get('dependencies',[])) \
 for p in pathlib.Path('/tmp/tools').glob('**/pyproject.toml')]; \
open('/tmp/pd.txt','w').write('\n'.join(sorted(deps)))" \
    && uv pip install -r /tmp/pd.txt --quiet \
    && rm /tmp/pd.txt

# 2. Copy source (changes often — keep last for fast rebuilds)
COPY services/api/api/ api/
COPY services/api/db/ db/
COPY tools/ tools/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --inexact

COPY services/api/bootstrap-secrets.sh /app/scripts/bootstrap-secrets.sh
COPY services/api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /app/scripts/bootstrap-secrets.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/app/.venv/bin/uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
