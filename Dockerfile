FROM ghcr.io/astral-sh/uv:0.11-debian-slim

# TLS-Zertifikate für die Mistral-Server, die das Basis-Image nicht mitbringt.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY . .
RUN uv sync --no-dev \
 && useradd --create-home --shell /usr/sbin/nologin agent \
 && chown -R agent:agent /app

USER agent

# Sessions sollen möglichst lange leben (Maximum für verwaltete Deployments: 24h idle, 1 Woche gesamt).
ENV AGENTS_SDK_IDLE_TIMEOUT_SECONDS=86400 \
    AGENTS_SDK_LIFETIME_LIMIT_SECONDS=604800 \
    AGENTS_SDK_EXECUTION_TIMEOUT_SECONDS=691200

CMD ["agents", "run-worker"]
