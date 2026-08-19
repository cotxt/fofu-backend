FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Keep the runtime identity stable so the production host can grant the
# persistent upload directory to this unprivileged user without guessing an
# image-assigned UID/GID.
RUN groupadd --gid 10001 fofu \
    && useradd --uid 10001 --gid fofu --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin fofu

COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --upgrade pip \
    && pip install '.[postgres]'

RUN mkdir -p /app/var/uploads \
    && chmod 0700 /app/var/uploads \
    && chown -R fofu:fofu /app
USER fofu

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
