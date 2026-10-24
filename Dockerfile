FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade openssl libssl3t64 libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2

WORKDIR /app

COPY pyproject.toml README.md ./
COPY connic_bridge/ connic_bridge/


COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
RUN uv pip install --system --no-cache --exclude-newer "3 days" .
RUN rm /bin/uv

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["connic-bridge"]
