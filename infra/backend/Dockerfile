FROM python:3.14.10-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend
COPY backend/requirements/dev-linux-py314.txt requirements/dev-linux-py314.txt
# This local development image deliberately reuses the existing hashed dev lock.
RUN python -m pip install --no-cache-dir --require-hashes -r requirements/dev-linux-py314.txt
COPY backend/pyproject.toml ./
COPY backend/src ./src
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -m pip check \
    && useradd --uid 10001 --user-group --no-create-home financeiq

USER 10001:10001
ENTRYPOINT ["python", "-m", "financeiq.core.container"]
CMD ["serve"]
