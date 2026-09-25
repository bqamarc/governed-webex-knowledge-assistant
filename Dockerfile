FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip build \
    && python -m build --wheel

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/assistant/.local/bin:$PATH

RUN useradd --create-home --uid 10001 assistant
WORKDIR /app
COPY --from=build /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
COPY alembic.ini ./
COPY migrations ./migrations
COPY examples ./examples
USER assistant
EXPOSE 8000
CMD ["webex-knowledge-assistant", "api"]
