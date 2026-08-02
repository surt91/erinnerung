FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Abhängigkeiten getrennt vom Quellcode installieren, damit der Layer-Cache greift.
# Bewusst ohne BuildKit-Cache-Mounts, damit auch der klassische Builder durchläuft.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ERINNERUNG_CONFIG_DIR=/config \
    ERINNERUNG_STATE_DB=/data/state.db

# Nicht als root laufen; /data gehört dem Volume.
RUN useradd --create-home --uid 1000 erinnerung \
    && mkdir -p /config /data \
    && chown erinnerung:erinnerung /config /data

WORKDIR /app
COPY --from=build --chown=erinnerung:erinnerung /app/.venv /app/.venv
COPY --from=build --chown=erinnerung:erinnerung /app/src /app/src

USER erinnerung

CMD ["python", "-m", "erinnerung"]
