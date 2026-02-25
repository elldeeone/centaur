FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install core dependencies
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ src/
RUN uv sync --frozen --no-dev

# Copy plugins
COPY plugins/ plugins/

# Install all plugin dependencies at build time
RUN python -c "import tomllib, pathlib; deps = set(); [deps.update(tomllib.load(open(p,'rb')).get('project',{}).get('dependencies',[])) for p in pathlib.Path('plugins').glob('*/pyproject.toml')]; open('/tmp/pd.txt','w').write('\n'.join(sorted(deps)))" && uv pip install -r /tmp/pd.txt --quiet && rm /tmp/pd.txt

# Copy migrations
COPY migrations/ migrations/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
