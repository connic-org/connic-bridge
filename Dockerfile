FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY connic_bridge/ connic_bridge/


COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /bin/uv
RUN uv pip install --system --no-cache --exclude-newer "3 days" .

ENV PYTHONUNBUFFERED=1

# Health check on port 8080 (optional, for container orchestrators)
EXPOSE 8080

ENTRYPOINT ["connic-bridge"]
