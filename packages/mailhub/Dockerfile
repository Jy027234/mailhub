# One image supports the sandbox graph in development and the fail-closed
# PostgreSQL/Host-Port graph in production-like environments. Runtime settings
# select the graph; production never falls back to Sandbox or in-memory state.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY scripts/apply_migrations.py ./scripts/apply_migrations.py
RUN python -m pip install --no-cache-dir ".[postgres]"

USER nobody
EXPOSE 8000
CMD ["mailhub-api", "--host", "0.0.0.0", "--port", "8000"]
