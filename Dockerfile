FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY requirements-search.lock requirements-ocr.lock requirements-observability.lock ./
ARG WITH_DOCUMENT_SERVICES=0
RUN if [ "$WITH_DOCUMENT_SERVICES" = 1 ]; then \
      apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0t64 && \
      rm -rf /var/lib/apt/lists/* && \
      pip install --no-cache-dir -r requirements-search.lock -r requirements-ocr.lock; \
    fi
ARG WITH_OBSERVABILITY=0
RUN if [ "$WITH_OBSERVABILITY" = 1 ]; then pip install --no-cache-dir -r requirements-observability.lock; fi
COPY requirements-s3.lock ./
ARG WITH_OBJECT_STORAGE=0
RUN if [ "$WITH_OBJECT_STORAGE" = 1 ]; then pip install --no-cache-dir -r requirements-s3.lock; fi
COPY procurepilot/ ./procurepilot/
COPY --from=frontend /build/dist ./frontend/dist
RUN useradd --uid 10001 --create-home pilot && mkdir /data && chown pilot /data
USER pilot
ENV DATABASE_URL=sqlite:////data/procurepilot.db
ENV OBJECT_ROOT=/data/objects EMBEDDING_CACHE_DIR=/data/embedding-cache
EXPOSE 8091
CMD ["uvicorn", "procurepilot.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091"]
