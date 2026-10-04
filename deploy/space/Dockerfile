# ThriftRadar demo (Hugging Face Docker Space). Spec: DESIGN.md §4.10, build step 15 decisions.
# Postgres runs inside this container (Q1 option C); the seed DB is built here and restored at every boot.

FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# System libraries for OpenCV (ultralytics, RapidOCR), plus Postgres 16 + pgvector from PGDG
# (bookworm's own pgvector is too old for HNSW indexes).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git libgl1 libglib2.0-0 \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16 postgresql-16-pgvector \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/usr/lib/postgresql/16/bin:$PATH

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

# Model weights baked in (no downloads at runtime). CLIP is needed only to bake YOLO-World's vocabulary.
# Only the pipeline modules the bake imports are copied first, so app changes don't re-bake (~1 GB download).
ENV YOLO_CONFIG_DIR=/tmp/ultralytics
COPY backend/app/__init__.py ./app/__init__.py
COPY backend/app/pipeline ./app/pipeline
COPY backend/scripts/__init__.py backend/scripts/bake_models.py ./scripts/
RUN pip install "git+https://github.com/ultralytics/CLIP.git@a13192f8cb767260d7dfd98c843b0716593169e7" \
 && python -m scripts.bake_models --out /models \
 && pip uninstall -y clip \
 && rm -rf /root/.cache /tmp/ultralytics \
 && chmod -R a+rX /models  # save_pretrained writes the weights 0600; the app runs as uid 1000

COPY backend/app ./app
COPY backend/scripts ./scripts

# The seed DB: built once here, restored at every boot.
COPY backend/seed ./seed
COPY build_seed.sh entrypoint.sh /usr/local/bin/
RUN useradd -m -u 1000 user \
 && chmod 755 /usr/local/bin/build_seed.sh /usr/local/bin/entrypoint.sh \
 && chown -R user /app
USER user
RUN test -r /models/siglip/model.safetensors && test -r /models/yolow-shoe.pt  # fail early if unreadable
ENV DEMO_MODE=1 MODELS_DIR=/models HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 YOLO_OFFLINE=1 \
    TRUST_PROXY_HOPS=1 PORT=7860 \
    SENDER_HMAC_KEY=thriftradar-demo-constant \
    VLM_PROVIDER=featherless VLM_MODEL=Qwen/Qwen3-VL-32B-Instruct \
    VLM_PRICE_IN_PER_M=0.104 VLM_PRICE_OUT_PER_M=0.416 \
    VLM_CONCURRENCY=2 VLM_RPM=20 VLM_DAILY_CAP=100
RUN build_seed.sh /app/seed.dump

COPY --from=web --chown=user /web/out /app/web/out
EXPOSE 7860
ENTRYPOINT ["entrypoint.sh"]
