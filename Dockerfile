FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

WORKDIR /app

RUN addgroup --system app \
    && adduser --system --ingroup app app

COPY pyproject.toml ./

RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import subprocess, sys, tomllib; dependencies = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]; subprocess.check_call([sys.executable, "-m", "pip", "install", *dependencies])'

COPY README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY --chmod=755 docker/curl-healthcheck.py /usr/local/bin/curl

RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps .

RUN mkdir -p /data/receipts && chown -R app:app /app /data
USER app

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn family_bot.main:app --host 0.0.0.0 --port 8000"]
