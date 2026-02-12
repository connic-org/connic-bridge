FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY connic_bridge/ connic_bridge/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

# Health check on port 8080 (optional, for container orchestrators)
EXPOSE 8080

ENTRYPOINT ["connic-bridge"]
