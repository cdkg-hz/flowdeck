FROM node:25-bookworm-slim AS web
WORKDIR /build
RUN npm install --global pnpm@11.19.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
LABEL org.opencontainers.image.title="Flowdeck" \
      org.opencontainers.image.description="Self-hosted GitHub development and release workflow" \
      org.opencontainers.image.licenses="MIT"
COPY LICENSE ./LICENSE
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime
RUN python -m copilot download-runtime \
    && chmod -R a+rX /opt/copilot-runtime
ENV COPILOT_SKIP_CLI_DOWNLOAD=1
RUN useradd --uid 1000 --create-home flowdeck
COPY backend/ backend/
COPY scripts/ scripts/
COPY config/ config/
COPY --from=web /build/dist frontend/dist/
USER flowdeck
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
