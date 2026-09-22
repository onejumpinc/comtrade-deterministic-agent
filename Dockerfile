FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

RUN apt-get update \
    && apt-get install --no-install-recommends --yes curl gosu \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos '' --uid 1000 agent

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY purple_agent.py run_a2a.py ./
COPY docker-entrypoint.sh /usr/local/bin/comtrade-entrypoint
RUN chmod 0755 /usr/local/bin/comtrade-entrypoint \
    && mkdir -p /workspace/purple_output \
    && chown -R agent:agent /app /workspace/purple_output

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

ARG VCS_REF=""
ARG SOURCE_URL=""
LABEL org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="${SOURCE_URL}"

ENTRYPOINT ["/usr/local/bin/comtrade-entrypoint"]
CMD ["--host", "0.0.0.0", "--port", "9009"]
EXPOSE 9009

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl --fail --silent http://localhost:9009/.well-known/agent-card.json >/dev/null
