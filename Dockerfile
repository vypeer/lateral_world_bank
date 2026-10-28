FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
COPY sql ./sql
# Mount output/ (and optionally data/) to keep results on the host
CMD ["uv", "run", "--no-dev", "wbpipe", "run"]
