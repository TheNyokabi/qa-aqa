#!/usr/bin/env bash
# QA/AQA Infrastructure — Podman foundation + data plane + observability
# Single-file source of truth. Edit, save, rerun.
#
# Spec: docs/superpowers/specs/2026-06-25-podman-foundation-data-observability-design.md
#
# Usage:
#   ./infra/infra.sh up        # default — install/start everything
#   ./infra/infra.sh down      # stop stack (keep volumes)
#   ./infra/infra.sh nuke      # stop + delete volumes (destructive)
#   ./infra/infra.sh status    # show service health
#   ./infra/infra.sh smoke     # run smoke tests only
#   ./infra/infra.sh dry-run   # print plan, change nothing

set -euo pipefail

# =============================================================================
# SECTION 1 — Variables (edit me when bumping versions or sizing)
# =============================================================================

readonly STACK_NAME="qa-aqa"
readonly NETWORK_NAME="qa-aqa"
readonly SANDBOX_NETWORK="sandbox-egress"  # D1.2 — isolated from qa-aqa
readonly INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"
readonly DIST_DIR="${INFRA_DIR}/dist"
readonly ENV_FILE="${INFRA_DIR}/.env"
# Used inside compose.yaml heredoc; expands to host path of repo root for bind mounts.
readonly INFRA_ROOT_FOR_COMPOSE="${REPO_ROOT}"

# Image versions (single place to bump)
readonly IMG_POSTGRES="docker.io/pgvector/pgvector:pg16"
readonly IMG_OPENSEARCH="docker.io/opensearchproject/opensearch:2"
readonly IMG_NEO4J="docker.io/library/neo4j:5-community"
readonly IMG_VALKEY="docker.io/valkey/valkey:7.2"
readonly IMG_TEMPORAL="docker.io/temporalio/auto-setup:1.24"
readonly IMG_TEMPORAL_UI="docker.io/temporalio/ui:2.51.1"
readonly IMG_OTEL="docker.io/otel/opentelemetry-collector-contrib:0.96.0"
readonly IMG_PROMETHEUS="docker.io/prom/prometheus:v2.51.0"
readonly IMG_GRAFANA="docker.io/grafana/grafana-oss:10.4.0"
readonly IMG_LOKI="docker.io/grafana/loki:3.0.0"
# Sub-project C1
readonly IMG_OLLAMA="docker.io/ollama/ollama:0.3.14"
readonly IMG_LITELLM="ghcr.io/berriai/litellm:main-stable"
readonly IMG_OPA="docker.io/openpolicyagent/opa:1.17.1"
# Sub-project C2 — locally built images
readonly IMG_RAG="qa-aqa/rag-service:dev"
readonly IMG_POLICY="qa-aqa/policy-svc:dev"
# Sub-project D0.5
readonly IMG_ARTEFACT="qa-aqa/artefact-service:dev"
# Sub-project D1
readonly IMG_AGENT_WORKERS="qa-aqa/agent-workers:dev"
# Sub-project D1.1
readonly IMG_MINIO="docker.io/minio/minio:RELEASE.2024-10-13T13-34-11Z"
readonly IMG_MINIO_MC="docker.io/minio/mc:RELEASE.2024-10-08T09-37-26Z"
readonly IMG_RUNNER="qa-aqa/runner-service:dev"
readonly IMG_SANDBOX="qa-aqa/sandbox:dev"   # D1.2 — Playwright Python pre-installed
readonly IMG_PROXY="qa-aqa/proxy-service:dev"  # D1.3 — mitmproxy + allowlist addon
# Sub-project D3a
readonly IMG_APISIX="docker.io/apache/apisix:3.10.0-debian"
readonly IMG_BFF="qa-aqa/bff:dev"
readonly IMG_WEB="qa-aqa/web-app:dev"
# NB: etcd was dropped in favour of APISIX standalone YAML mode.

# Models to pre-pull on Ollama after startup (idempotent — skips if present)
readonly OLLAMA_PRELOAD_MODELS=("nomic-embed-text" "llama3.2:3b")

# Podman machine sizing
readonly MACHINE_NAME="podman-machine-default"
readonly MACHINE_CPUS=6
readonly MACHINE_MEMORY=20480   # MB — bumped from 12288 for Sub-project C (Ollama needs ~6 GB)
readonly MACHINE_DISK=100       # GB

# Ports on the host (must be free)
readonly PORT_POSTGRES=55432   # host-facing port; container-internal is always 5432
                               # bumped off 5432 because Homebrew postgresql often binds it
readonly PORT_OPENSEARCH=9200
readonly PORT_NEO4J_HTTP=7474
readonly PORT_NEO4J_BOLT=7687
readonly PORT_VALKEY=6379
readonly PORT_TEMPORAL_GRPC=7233
readonly PORT_TEMPORAL_UI=8233
readonly PORT_OTEL_GRPC=4317
readonly PORT_OTEL_HTTP=4318
readonly PORT_OTEL_HEALTH=13133
readonly PORT_PROMETHEUS=9090
readonly PORT_GRAFANA=3000
readonly PORT_LOKI=3100
# Sub-project C1
readonly PORT_OLLAMA=11434
readonly PORT_LITELLM=4000
readonly PORT_OPA=8181
# Sub-project C2
readonly PORT_RAG=8001
readonly PORT_POLICY=8002
# Sub-project D0.5
readonly PORT_ARTEFACT=8003
# Sub-project D1.1
readonly PORT_RUNNER=8004
readonly PORT_MINIO_API=9101    # S3 API on host
readonly PORT_MINIO_WEB=9100    # Web console on host
# D1.3
readonly PORT_PROXY_ADMIN=8083  # mitmproxy admin endpoint exposed to host (debug)
# D3a
readonly PORT_APISIX_PROXY=9080
readonly PORT_APISIX_ADMIN=9180
readonly PORT_BFF=8005
readonly APISIX_ADMIN_KEY="qa-aqa-dev-admin-key"

# Brew packages required on the host
readonly BREW_PKGS=("podman" "podman-compose" "jq")

# Default .env values (only written if .env is missing)
readonly DEFAULT_POSTGRES_USER="app"
readonly DEFAULT_POSTGRES_PASSWORD="appdevpw"
readonly DEFAULT_POSTGRES_DB="app"
readonly DEFAULT_NEO4J_PASSWORD="neo4jdevpw"
readonly DEFAULT_GRAFANA_ADMIN_PASSWORD="admin"
# Cloud API keys default to empty — fill in your .env to enable cloud aliases
readonly DEFAULT_ANTHROPIC_API_KEY=""
readonly DEFAULT_OPENAI_API_KEY=""
# D1.1 — MinIO
readonly DEFAULT_MINIO_ROOT_USER="minio"
# password generated on first run; appended to .env if missing

# =============================================================================
# SECTION 2 — Log helpers
# =============================================================================

readonly C_RESET='\033[0m'
readonly C_BOLD='\033[1m'
readonly C_DIM='\033[2m'
readonly C_RED='\033[31m'
readonly C_GREEN='\033[32m'
readonly C_YELLOW='\033[33m'
readonly C_BLUE='\033[34m'

log()    { printf "${C_BLUE}▸${C_RESET} %s\n" "$*"; }
ok()     { printf "${C_GREEN}✔${C_RESET} %s\n" "$*"; }
warn()   { printf "${C_YELLOW}⚠${C_RESET} %s\n" "$*"; }
err()    { printf "${C_RED}✘${C_RESET} %s\n" "$*" >&2; }
step()   { printf "\n${C_BOLD}══ %s ══${C_RESET}\n" "$*"; }
dim()    { printf "${C_DIM}%s${C_RESET}\n" "$*"; }

DRY_RUN=0
run_or_print() {
    if [[ "${DRY_RUN}" == "1" ]]; then
        dim "[dry-run] $*"
    else
        eval "$@"
    fi
}

# =============================================================================
# SECTION 3 — Prereq install
# =============================================================================

ensure_brew_packages() {
    step "1/8  Prereqs"
    if ! command -v brew >/dev/null 2>&1; then
        err "Homebrew not found. Install from https://brew.sh first."
        exit 1
    fi
    for pkg in "${BREW_PKGS[@]}"; do
        if brew list --formula --versions "$pkg" >/dev/null 2>&1; then
            ok "brew: $pkg already installed"
        else
            log "brew install $pkg"
            run_or_print "brew install '$pkg'"
        fi
    done
}

# =============================================================================
# SECTION 4 — Podman machine
# =============================================================================

ensure_podman_machine() {
    step "2/8  Podman machine"
    if ! podman machine list --format json 2>/dev/null | jq -e ".[] | select(.Name==\"${MACHINE_NAME}\")" >/dev/null; then
        log "Initialising machine (~2 GB download, several minutes)"
        run_or_print "podman machine init --cpus ${MACHINE_CPUS} --memory ${MACHINE_MEMORY} --disk-size ${MACHINE_DISK} '${MACHINE_NAME}'"
    else
        ok "machine: ${MACHINE_NAME} exists"
    fi

    # Resize RAM if it doesn't match target (e.g. after a sub-project bumps it).
    # Resources.Memory is reported in MB by podman.
    local current_mem_mb
    current_mem_mb=$(podman machine inspect "${MACHINE_NAME}" 2>/dev/null | jq -r '.[0].Resources.Memory // 0')
    if [[ -n "${current_mem_mb}" && "${current_mem_mb}" != "0" && "${current_mem_mb}" != "${MACHINE_MEMORY}" ]]; then
        warn "machine memory ${current_mem_mb} MB != target ${MACHINE_MEMORY} MB — resizing (containers will need to be restarted via 'compose up' after)"
        run_or_print "podman machine stop '${MACHINE_NAME}' || true"
        run_or_print "podman machine set --memory ${MACHINE_MEMORY} '${MACHINE_NAME}'"
    elif [[ "${current_mem_mb}" == "${MACHINE_MEMORY}" ]]; then
        ok "machine memory: ${current_mem_mb} MB (target)"
    fi

    local running
    running=$(podman machine list --format json 2>/dev/null | jq -r ".[] | select(.Name==\"${MACHINE_NAME}\") | .Running" || echo "false")
    if [[ "${running}" != "true" ]]; then
        log "Starting machine"
        run_or_print "podman machine start '${MACHINE_NAME}' || true"
    else
        ok "machine: running"
    fi
}

# =============================================================================
# SECTION 5 — Generate configs
# =============================================================================

write_dist_dir() {
    step "3/8  Generate configs in ${DIST_DIR}"
    mkdir -p \
        "${DIST_DIR}/postgres-init" \
        "${DIST_DIR}/otel" \
        "${DIST_DIR}/prometheus" \
        "${DIST_DIR}/grafana/provisioning/datasources" \
        "${DIST_DIR}/loki" \
        "${DIST_DIR}/litellm" \
        "${DIST_DIR}/opa/policies/qa_aqa" \
        "${DIST_DIR}/apisix"

    cat >"${DIST_DIR}/postgres-init/01-temporal.sql" <<'EOF'
-- Create temporal user + databases for the temporal auto-setup container
-- CREATEDB needed because auto-setup's schema tool issues CREATE DATABASE
CREATE USER temporal WITH PASSWORD 'temporal' CREATEDB;
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
GRANT ALL PRIVILEGES ON DATABASE temporal TO temporal;
GRANT ALL PRIVILEGES ON DATABASE temporal_visibility TO temporal;
-- pgvector extension on the app database
\c app
CREATE EXTENSION IF NOT EXISTS vector;
EOF
    ok "wrote postgres-init/01-temporal.sql"

    cat >"${DIST_DIR}/otel/config.yaml" <<EOF
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:${PORT_OTEL_GRPC}
      http:
        endpoint: 0.0.0.0:${PORT_OTEL_HTTP}

processors:
  batch: {}

exporters:
  prometheus:
    endpoint: 0.0.0.0:8889
  loki:
    endpoint: http://loki:${PORT_LOKI}/loki/api/v1/push
  debug: {}

extensions:
  health_check:
    endpoint: 0.0.0.0:${PORT_OTEL_HEALTH}

service:
  extensions: [health_check]
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus, debug]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [loki, debug]
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
EOF
    ok "wrote otel/config.yaml"

    cat >"${DIST_DIR}/prometheus/prometheus.yml" <<EOF
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:${PORT_PROMETHEUS}']
  - job_name: 'otel-collector'
    static_configs:
      - targets: ['otel-collector:8889']
EOF
    ok "wrote prometheus/prometheus.yml"

    cat >"${DIST_DIR}/grafana/provisioning/datasources/datasources.yml" <<EOF
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:${PORT_PROMETHEUS}
    isDefault: true
  - name: Loki
    type: loki
    access: proxy
    url: http://loki:${PORT_LOKI}
EOF
    ok "wrote grafana/provisioning/datasources/datasources.yml"

    cat >"${DIST_DIR}/loki/config.yaml" <<EOF
auth_enabled: false

server:
  http_listen_port: ${PORT_LOKI}

common:
  instance_addr: 127.0.0.1
  path_prefix: /tmp/loki
  storage:
    filesystem:
      chunks_directory: /tmp/loki/chunks
      rules_directory: /tmp/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

ruler:
  alertmanager_url: http://localhost:9093
EOF
    ok "wrote loki/config.yaml"

    cat >"${DIST_DIR}/litellm/config.yaml" <<EOF
# Aliases hide the backend from callers. Switch dev↔prod by changing alias mapping.
model_list:
  - model_name: chat-dev
    litellm_params:
      model: ollama_chat/llama3.2:3b
      api_base: http://ollama:${PORT_OLLAMA}

  - model_name: chat-prod
    litellm_params:
      model: anthropic/claude-opus-4-7
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: embed-dev
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://ollama:${PORT_OLLAMA}

  - model_name: embed-prod
    litellm_params:
      model: openai/text-embedding-3-small
      api_key: os.environ/OPENAI_API_KEY

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
EOF
    ok "wrote litellm/config.yaml"

    # APISIX in standalone YAML mode — no etcd dependency for v1 dev.
    cat >"${DIST_DIR}/apisix/config.yaml" <<EOF
apisix:
  node_listen: 9080
  enable_admin: false   # standalone mode; admin API disabled
deployment:
  role: data_plane
  role_data_plane:
    config_provider: yaml
EOF
    ok "wrote apisix/config.yaml"

    # APISIX standalone routes file. The trailing #END marker is required.
    cat >"${DIST_DIR}/apisix/apisix.yaml" <<'EOF'
routes:
  - id: api-bff
    uri: /api/*
    upstream:
      type: roundrobin
      nodes:
        bff:8005: 1
  - id: web-spa
    uri: /*
    priority: 0
    upstream:
      type: roundrobin
      nodes:
        web-app:80: 1
#END
EOF
    ok "wrote apisix/apisix.yaml"

    cat >"${DIST_DIR}/opa/policies/qa_aqa/authz.rego" <<'EOF'
package qa_aqa.authz

default allow := false

# Admins can do anything
allow if {
    input.subject.role == "admin"
}

# Anyone can read public resources
allow if {
    input.action == "read"
    input.resource.visibility == "public"
}
EOF
    ok "wrote opa/policies/qa_aqa/authz.rego"

    write_compose_yaml
}

write_compose_yaml() {
    cat >"${DIST_DIR}/compose.yaml" <<EOF
# Generated by infra.sh — do not edit by hand. Edit infra.sh and rerun.
name: ${STACK_NAME}

networks:
  ${NETWORK_NAME}:
    name: ${NETWORK_NAME}
  ${SANDBOX_NETWORK}:
    name: ${SANDBOX_NETWORK}
    # Sandboxes attach to this network only. NOT connected to ${NETWORK_NAME},
    # so they cannot resolve or reach any internal service by name.

volumes:
  pg-data:
  os-data:
  neo4j-data:
  valkey-data:
  prom-data:
  grafana-data:
  loki-data:
  ollama-data:
  minio-data:
  runner-tmp:
  mitm-ca:           # D1.3 — persists mitmproxy's CA between restarts

services:
  postgres:
    image: ${IMG_POSTGRES}
    container_name: postgres
    environment:
      POSTGRES_USER: \${POSTGRES_USER}
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
      POSTGRES_DB: \${POSTGRES_DB}
    ports:
      - "${PORT_POSTGRES}:5432"
    volumes:
      - pg-data:/var/lib/postgresql/data
      - ./postgres-init:/docker-entrypoint-initdb.d:Z
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U \${POSTGRES_USER}"]
      interval: 5s
      timeout: 5s
      retries: 20

  opensearch:
    image: ${IMG_OPENSEARCH}
    container_name: opensearch
    environment:
      - cluster.name=qa-aqa-cluster
      - node.name=opensearch
      - discovery.type=single-node
      - bootstrap.memory_lock=true
      - DISABLE_SECURITY_PLUGIN=true
      - DISABLE_INSTALL_DEMO_CONFIG=true
      - OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g
    ulimits:
      memlock:
        soft: -1
        hard: -1
    ports:
      - "${PORT_OPENSEARCH}:9200"
    volumes:
      - os-data:/usr/share/opensearch/data
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:9200/_cluster/health || exit 1"]
      interval: 10s
      timeout: 10s
      retries: 30

  neo4j:
    image: ${IMG_NEO4J}
    container_name: neo4j
    environment:
      - NEO4J_AUTH=neo4j/\${NEO4J_PASSWORD}
      - NEO4J_dbms_memory_heap_initial__size=512m
      - NEO4J_dbms_memory_heap_max__size=1g
    ports:
      - "${PORT_NEO4J_HTTP}:7474"
      - "${PORT_NEO4J_BOLT}:7687"
    volumes:
      - neo4j-data:/data
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:7474 || exit 1"]
      interval: 10s
      timeout: 10s
      retries: 20

  valkey:
    image: ${IMG_VALKEY}
    container_name: valkey
    ports:
      - "${PORT_VALKEY}:6379"
    volumes:
      - valkey-data:/data
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD", "valkey-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  temporal:
    image: ${IMG_TEMPORAL}
    container_name: temporal
    environment:
      - DB=postgres12
      - DB_PORT=5432
      - POSTGRES_USER=temporal
      - POSTGRES_PWD=temporal
      - POSTGRES_SEEDS=postgres
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "${PORT_TEMPORAL_GRPC}:7233"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "tctl --address temporal:7233 cluster health || exit 0"]
      interval: 10s
      timeout: 10s
      retries: 30

  temporal-ui:
    image: ${IMG_TEMPORAL_UI}
    container_name: temporal-ui
    environment:
      - TEMPORAL_ADDRESS=temporal:7233
      - TEMPORAL_CORS_ORIGINS=http://localhost:${PORT_GRAFANA}
    depends_on:
      - temporal
    ports:
      - "${PORT_TEMPORAL_UI}:8080"
    networks: [${NETWORK_NAME}]

  otel-collector:
    image: ${IMG_OTEL}
    container_name: otel-collector
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./otel/config.yaml:/etc/otelcol/config.yaml:Z
    ports:
      - "${PORT_OTEL_GRPC}:${PORT_OTEL_GRPC}"
      - "${PORT_OTEL_HTTP}:${PORT_OTEL_HTTP}"
      - "${PORT_OTEL_HEALTH}:${PORT_OTEL_HEALTH}"
    networks: [${NETWORK_NAME}]
    # No in-container healthcheck — distroless image lacks curl/wget.
    # Smoke test in infra.sh verifies the /  endpoint from the host.

  prometheus:
    image: ${IMG_PROMETHEUS}
    container_name: prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.enable-lifecycle'
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:Z
      - prom-data:/prometheus
    ports:
      - "${PORT_PROMETHEUS}:9090"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:9090/-/healthy || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10

  grafana:
    image: ${IMG_GRAFANA}
    container_name: grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=\${GRAFANA_ADMIN_PASSWORD}
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:Z
      - grafana-data:/var/lib/grafana
    ports:
      - "${PORT_GRAFANA}:3000"
    depends_on:
      - prometheus
      - loki
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:3000/api/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10

  loki:
    image: ${IMG_LOKI}
    container_name: loki
    command: ["-config.file=/etc/loki/config.yaml"]
    volumes:
      - ./loki/config.yaml:/etc/loki/config.yaml:Z
      - loki-data:/tmp/loki
    ports:
      - "${PORT_LOKI}:3100"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:3100/ready || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10

  # ─── Sub-project C1 ───────────────────────────────────────────────────────
  ollama:
    image: ${IMG_OLLAMA}
    container_name: ollama
    environment:
      # Keep loaded models in memory for 24h after last use so the next
      # workflow doesn't pay a cold-start tax of ~30-60s + degraded inference.
      - OLLAMA_KEEP_ALIVE=24h
    volumes:
      - ollama-data:/root/.ollama
    ports:
      - "${PORT_OLLAMA}:11434"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "ollama list >/dev/null 2>&1 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 30

  model-gateway:
    image: ${IMG_LITELLM}
    container_name: model-gateway
    command: ["--config", "/app/config.yaml", "--port", "${PORT_LITELLM}"]
    environment:
      - LITELLM_MASTER_KEY=\${LITELLM_MASTER_KEY}
      - ANTHROPIC_API_KEY=\${ANTHROPIC_API_KEY:-}
      - OPENAI_API_KEY=\${OPENAI_API_KEY:-}
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:Z
    ports:
      - "${PORT_LITELLM}:${PORT_LITELLM}"
    depends_on:
      - ollama
    networks: [${NETWORK_NAME}]
    # LiteLLM image is distroless (no wget/curl/sh); smoke test verifies /health/liveliness from host

  opa:
    image: ${IMG_OPA}
    container_name: opa
    command:
      - run
      - --server
      - --addr=0.0.0.0:${PORT_OPA}
      - --log-level=info
      - /policies
    volumes:
      - ./opa/policies:/policies:Z
    ports:
      - "${PORT_OPA}:${PORT_OPA}"
    networks: [${NETWORK_NAME}]
    # OPA's distroless image has no shell — rely on host-side smoke test

  # ─── Sub-project C2 ───────────────────────────────────────────────────────
  rag-service:
    image: ${IMG_RAG}
    container_name: rag-service
    environment:
      - PGHOST=postgres
      - PGPORT=5432
      - PGUSER=\${POSTGRES_USER}
      - PGPASSWORD=\${POSTGRES_PASSWORD}
      - PGDATABASE=\${POSTGRES_DB}
      - OPENSEARCH_URL=http://opensearch:9200
      - OPENSEARCH_INDEX=documents
      - MODEL_GATEWAY_URL=http://model-gateway:${PORT_LITELLM}
      - LITELLM_MASTER_KEY=\${LITELLM_MASTER_KEY}
      - EMBED_MODEL=embed-dev
      - EMBED_DIM=768
    depends_on:
      postgres:
        condition: service_healthy
      opensearch:
        condition: service_healthy
      model-gateway:
        condition: service_started
    ports:
      - "${PORT_RAG}:8001"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8001/health').read()\" || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  policy-svc:
    image: ${IMG_POLICY}
    container_name: policy-svc
    environment:
      - OPA_URL=http://opa:${PORT_OPA}
    depends_on:
      - opa
    ports:
      - "${PORT_POLICY}:8002"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8002/health').read()\" || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  # ─── Sub-project D3a — APISIX (standalone YAML) + BFF + web-app ─────────
  apisix:
    image: ${IMG_APISIX}
    container_name: apisix
    ports:
      - "${PORT_APISIX_PROXY}:9080"
    volumes:
      - ./apisix/config.yaml:/usr/local/apisix/conf/config.yaml:Z
      - ./apisix/apisix.yaml:/usr/local/apisix/conf/apisix.yaml:Z
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:9080/ || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 30

  bff:
    image: ${IMG_BFF}
    container_name: bff
    environment:
      - BFF_PORT=8005
      - ARTEFACT_URL=http://artefact-service:8003
      - TEMPORAL_HOST=temporal:7233
      - TEMPORAL_TASK_QUEUE=test-design
      - MINIO_ENDPOINT=minio:9000
      - MINIO_BUCKET=executions
      - MINIO_ROOT_USER=\${MINIO_ROOT_USER}
      - MINIO_ROOT_PASSWORD=\${MINIO_ROOT_PASSWORD}
      - BFF_JWT_SECRET=\${BFF_JWT_SECRET}
      - CORS_ALLOW_ORIGINS=http://localhost:${PORT_APISIX_PROXY},http://localhost:5173
    depends_on:
      artefact-service:
        condition: service_healthy
      temporal:
        condition: service_healthy
      minio:
        condition: service_healthy
    ports:
      - "${PORT_BFF}:8005"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8005/api/health').read()\" || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  web-app:
    image: ${IMG_WEB}
    container_name: web-app
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:80/ || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  # ─── Sub-project D1.3 — TLS-MITM egress proxy ────────────────────────────
  proxy-service:
    image: ${IMG_PROXY}
    container_name: proxy-service
    security_opt:
      - "label=disable"
    user: "0:0"
    volumes:
      - mitm-ca:/home/mitmproxy/.mitmproxy
      # Same VM-host bind-mount runner-service uses for allowlist configs.
      - /tmp/proxy-allowlists:/tmp/proxy-allowlists:ro
    ports:
      # Proxy listener (only useful from inside sandbox-egress; published for
      # debug introspection from the host).
      - "${PORT_PROXY_ADMIN}:8080"
    networks: [${SANDBOX_NETWORK}]
    healthcheck:
      # mitmproxy itself doesn't expose /health; we check by tcp-probing 8080.
      test: ["CMD-SHELL", "bash -c '</dev/tcp/127.0.0.1/8080' || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 30

  # ─── Sub-project D0.5 ─────────────────────────────────────────────────────
  artefact-service:
    image: ${IMG_ARTEFACT}
    container_name: artefact-service
    environment:
      - PGHOST=postgres
      - PGPORT=5432
      - PGUSER=\${POSTGRES_USER}
      - PGPASSWORD=\${POSTGRES_PASSWORD}
      - PGDATABASE=\${POSTGRES_DB}
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "${PORT_ARTEFACT}:8003"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8003/health').read()\" || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  # ─── Sub-project D1.1 — storage + runner ──────────────────────────────────
  minio:
    image: ${IMG_MINIO}
    container_name: minio
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      - MINIO_ROOT_USER=\${MINIO_ROOT_USER}
      - MINIO_ROOT_PASSWORD=\${MINIO_ROOT_PASSWORD}
    volumes:
      - minio-data:/data
    ports:
      - "${PORT_MINIO_API}:9000"
      - "${PORT_MINIO_WEB}:9001"
    networks: [${NETWORK_NAME}]
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:9000/minio/health/live || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  runner-service:
    image: ${IMG_RUNNER}
    container_name: runner-service
    # D1.2: SELinux in the Fedora image blocks access to the bind-mounted
    # podman socket. Disable container-side labelling (the socket lives on the
    # podman-machine VM which doesn't enforce SELinux on the mount target).
    security_opt:
      - "label=disable"
    environment:
      - MINIO_ENDPOINT=minio:9000
      - MINIO_BUCKET=executions
      - MINIO_ROOT_USER=\${MINIO_ROOT_USER}
      - MINIO_ROOT_PASSWORD=\${MINIO_ROOT_PASSWORD}
      - SANDBOX_TIMEOUT_DEFAULT=120
      - SANDBOX_IMAGE=${IMG_SANDBOX}
      - SANDBOX_NETWORK=${SANDBOX_NETWORK}
      - SANDBOX_TMP_BASE=/sandbox-tmp
      - CONTAINER_HOST=unix:///run/podman/podman.sock
      - SANDBOX_TMP_HOST_BASE=/tmp/runner-sandboxes
      - PROXY_URL=http://proxy-service:8080
      - MITM_CA_DIR=/mitm-ca
      - PROXY_ALLOWLIST_HOST_DIR=/tmp/proxy-allowlists
      - PROXY_ALLOWLIST_DIR=/proxy-allowlists
      # D1.4 — async queue + per-tenant quotas
      - VALKEY_URL=redis://valkey:6379
      - QUOTA_CONCURRENT_DEFAULT=3
      - QUOTA_DAILY_DEFAULT=100
    depends_on:
      minio:
        condition: service_healthy
      proxy-service:
        condition: service_healthy
      valkey:
        condition: service_healthy
    ports:
      - "${PORT_RUNNER}:8004"
    networks: [${NETWORK_NAME}]
    volumes:
      # D1.2: bind-mount the in-VM rootless podman socket so runner-service
      # can spawn ephemeral sandbox containers. SECURITY: gives runner-service
      # container-runtime root. The trust boundary is the FastAPI code itself.
      # Path inside the VM: rootless socket lives at /run/user/<core uid>/podman/podman.sock.
      # On macOS podman-machine the 'core' user has UID 501.
      - /run/user/501/podman/podman.sock:/run/podman/podman.sock
      # D1.2: shared dir for per-call sandbox tmp dirs (script + screenshots).
      # MUST be a VM-host bind-mount (not a named volume) because the inner
      # podman command needs to mount the same VM path into the sandbox.
      # The VM dir is created by infra.sh before compose up.
      - /tmp/runner-sandboxes:/sandbox-tmp
      # D1.3 — read-only CA cert mount (copied into sandbox per run)
      - mitm-ca:/mitm-ca:ro
      # D1.3 — per-sandbox allow-list configs that proxy-service reads
      - /tmp/proxy-allowlists:/proxy-allowlists
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8004/health').read()\" || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 20

  # ─── Sub-project D1 ───────────────────────────────────────────────────────
  agent-workers:
    image: ${IMG_AGENT_WORKERS}
    container_name: agent-workers
    environment:
      - TEMPORAL_HOST=temporal:7233
      - TEMPORAL_TASK_QUEUE=test-design
      - ARTEFACT_URL=http://artefact-service:8003
      - RAG_URL=http://rag-service:8001
      - POLICY_URL=http://policy-svc:8002
      - MODEL_GATEWAY_URL=http://model-gateway:4000
      - RUNNER_URL=http://runner-service:8004
      - LITELLM_MASTER_KEY=\${LITELLM_MASTER_KEY}
      - ANTHROPIC_API_KEY=\${ANTHROPIC_API_KEY:-}
      - DOCS_SOURCE=/specs
    depends_on:
      temporal:
        condition: service_healthy
      artefact-service:
        condition: service_healthy
      rag-service:
        condition: service_healthy
      ollama:
        condition: service_healthy
      runner-service:
        condition: service_healthy
    volumes:
      - ${INFRA_ROOT_FOR_COMPOSE}/docs/superpowers/specs:/specs:ro
    networks: [${NETWORK_NAME}]
    # No HTTP -> no container healthcheck. Smoke verifies via temporal CLI calls.
EOF
    ok "wrote compose.yaml"
}

# =============================================================================
# SECTION 6 — .env (never overwrites)
# =============================================================================

ensure_env_file() {
    step "4/8  Secrets (${ENV_FILE})"
    if [[ ! -f "${ENV_FILE}" ]]; then
        local litellm_master_key
        litellm_master_key="sk-$(openssl rand -hex 24)"
        local minio_pw
    minio_pw="$(openssl rand -hex 24)"
    cat >"${ENV_FILE}" <<EOF
# QA/AQA infra secrets — generated by infra.sh
# Edit if you want; missing keys are appended on rerun, existing keys preserved.
POSTGRES_USER=${DEFAULT_POSTGRES_USER}
POSTGRES_PASSWORD=${DEFAULT_POSTGRES_PASSWORD}
POSTGRES_DB=${DEFAULT_POSTGRES_DB}
NEO4J_PASSWORD=${DEFAULT_NEO4J_PASSWORD}
GRAFANA_ADMIN_PASSWORD=${DEFAULT_GRAFANA_ADMIN_PASSWORD}
LITELLM_MASTER_KEY=${litellm_master_key}
ANTHROPIC_API_KEY=${DEFAULT_ANTHROPIC_API_KEY}
OPENAI_API_KEY=${DEFAULT_OPENAI_API_KEY}
MINIO_ROOT_USER=${DEFAULT_MINIO_ROOT_USER}
MINIO_ROOT_PASSWORD=${minio_pw}
BFF_JWT_SECRET=$(openssl rand -hex 32)
EOF
        chmod 600 "${ENV_FILE}"
        ok ".env created with defaults"
        return
    fi
    # File exists — append any missing keys without disturbing existing ones
    local appended=0
    _append_if_missing() {
        local key="$1" val="$2"
        if ! grep -qE "^${key}=" "${ENV_FILE}"; then
            printf '%s=%s\n' "${key}" "${val}" >>"${ENV_FILE}"
            log "appended ${key} to .env"
            appended=$((appended+1))
        fi
    }
    _append_if_missing POSTGRES_USER "${DEFAULT_POSTGRES_USER}"
    _append_if_missing POSTGRES_PASSWORD "${DEFAULT_POSTGRES_PASSWORD}"
    _append_if_missing POSTGRES_DB "${DEFAULT_POSTGRES_DB}"
    _append_if_missing NEO4J_PASSWORD "${DEFAULT_NEO4J_PASSWORD}"
    _append_if_missing GRAFANA_ADMIN_PASSWORD "${DEFAULT_GRAFANA_ADMIN_PASSWORD}"
    _append_if_missing LITELLM_MASTER_KEY "sk-$(openssl rand -hex 24)"
    _append_if_missing ANTHROPIC_API_KEY "${DEFAULT_ANTHROPIC_API_KEY}"
    _append_if_missing OPENAI_API_KEY "${DEFAULT_OPENAI_API_KEY}"
    _append_if_missing MINIO_ROOT_USER "${DEFAULT_MINIO_ROOT_USER}"
    _append_if_missing MINIO_ROOT_PASSWORD "$(openssl rand -hex 24)"
    _append_if_missing BFF_JWT_SECRET "$(openssl rand -hex 32)"
    if (( appended == 0 )); then
        ok ".env already complete"
    else
        ok ".env: appended ${appended} missing key(s)"
    fi
}

# =============================================================================
# SECTION 7 — Bring stack up
# =============================================================================

compose() {
    podman-compose --env-file "${ENV_FILE}" -f "${DIST_DIR}/compose.yaml" "$@"
}

build_custom_images() {
    step "5a/8  Build custom images (rag, policy, artefact, agent-workers, runner, sandbox, proxy, bff, web)"
    local services=("rag" "policy" "artefact" "agent-workers" "runner" "sandbox-image" "proxy" "bff" "web-app")
    local images=("${IMG_RAG}" "${IMG_POLICY}" "${IMG_ARTEFACT}" "${IMG_AGENT_WORKERS}" "${IMG_RUNNER}" "${IMG_SANDBOX}" "${IMG_PROXY}" "${IMG_BFF}" "${IMG_WEB}")
    for i in "${!services[@]}"; do
        local svc="${services[$i]}"
        local img="${images[$i]}"
        # Lookup: services/<svc> first, then clients/<svc-stripped> for the web app
        local ctx="${INFRA_DIR%/infra}/services/${svc}"
        if [[ ! -d "${ctx}" && "${svc}" == "web-app" ]]; then
            ctx="${INFRA_DIR%/infra}/clients/web"
        fi
        if [[ ! -d "${ctx}" ]]; then
            warn "no source at ${ctx} — skipping ${img}"
            continue
        fi
        log "podman build ${img}"
        run_or_print "podman build -t '${img}' -f '${ctx}/Containerfile' '${ctx}'"
    done
}

ensure_networks() {
    # D1.2 — sandbox-egress must exist before compose up so runner-service's
    # references resolve. podman network create is idempotent enough that this
    # is safe to call repeatedly.
    if ! podman network exists "${SANDBOX_NETWORK}" 2>/dev/null; then
        log "creating network: ${SANDBOX_NETWORK}"
        run_or_print "podman network create '${SANDBOX_NETWORK}' >/dev/null"
    else
        ok "network: ${SANDBOX_NETWORK} exists"
    fi
}

ensure_socket_readable() {
    # D1.2 — chmod the rootless podman socket so the bind-mount inside
    # runner-service can connect. The socket is owned by core:core (UID 501)
    # but runner-service runs as root which user-namespaces to a different UID.
    # The simple/portable fix is to widen the socket mode for dev. Production
    # would use a system socket activation unit with a known UID/GID.
    log "ensuring podman socket is world-accessible inside the VM"
    run_or_print "podman machine ssh 'sudo chmod 666 /run/user/501/podman/podman.sock' >/dev/null 2>&1 || true"
}

ensure_sandbox_tmp_dir() {
    # D1.2 — sandbox tmp dirs must live on a path visible to the VM (where the
    # podman daemon spawns sandbox containers), not just inside runner-service.
    log "ensuring sandbox tmp dir exists on VM: /tmp/runner-sandboxes"
    run_or_print "podman machine ssh 'sudo mkdir -p /tmp/runner-sandboxes && sudo chmod 1777 /tmp/runner-sandboxes' >/dev/null 2>&1 || true"
    # D1.3 — per-sandbox proxy allow-list dir, shared between runner-service
    # (writes) and proxy-service (reads).
    log "ensuring proxy allow-list dir exists on VM: /tmp/proxy-allowlists"
    run_or_print "podman machine ssh 'sudo mkdir -p /tmp/proxy-allowlists && sudo chmod 1777 /tmp/proxy-allowlists' >/dev/null 2>&1 || true"
}

bring_up() {
    step "5/8  Pull images + start stack"
    ensure_networks
    ensure_socket_readable
    ensure_sandbox_tmp_dir
    run_or_print "(cd '${DIST_DIR}' && podman-compose --env-file '${ENV_FILE}' -f compose.yaml up -d)"
}

ollama_preload() {
    step "5b/8  Pre-pull Ollama models (idempotent)"
    if ! podman exec ollama ollama list >/dev/null 2>&1; then
        warn "ollama container not responsive yet, sleeping 5s"
        sleep 5
    fi
    for model in "${OLLAMA_PRELOAD_MODELS[@]}"; do
        if podman exec ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qE "^${model}(:|$)"; then
            ok "ollama: ${model} already present"
        else
            log "ollama pull ${model} (this can take minutes for the first model)"
            run_or_print "podman exec ollama ollama pull '${model}'"
        fi
    done
}

bring_down() {
    step "Down"
    (cd "${DIST_DIR}" && podman-compose --env-file "${ENV_FILE}" -f compose.yaml down)
}

nuke() {
    step "Nuke (DESTRUCTIVE — wipes volumes)"
    (cd "${DIST_DIR}" && podman-compose --env-file "${ENV_FILE}" -f compose.yaml down -v) || true
    for vol in pg-data os-data neo4j-data valkey-data prom-data grafana-data loki-data; do
        podman volume rm "${STACK_NAME}_${vol}" 2>/dev/null || true
    done
    ok "volumes removed"
}

# =============================================================================
# SECTION 8 — Wait + smoke tests
# =============================================================================

wait_healthy() {
    step "6/8  Wait for services healthy"
    local services=(postgres opensearch neo4j valkey prometheus grafana loki otel-collector temporal temporal-ui ollama model-gateway opa rag-service policy-svc artefact-service minio proxy-service runner-service agent-workers apisix bff web-app)
    local max_wait=300
    local elapsed=0
    while (( elapsed < max_wait )); do
        local unhealthy=0
        for svc in "${services[@]}"; do
            local state
            state=$(podman inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$svc" 2>/dev/null || echo "missing")
            if [[ "$state" != "healthy" && "$state" != "running" ]]; then
                unhealthy=1
            fi
        done
        if (( unhealthy == 0 )); then
            ok "all services healthy"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        printf "."
    done
    err "timed out waiting for services after ${max_wait}s"
    podman ps --format "table {{.Names}}\t{{.Status}}"
    return 1
}

smoke_artefact_d05_suite() {
    # Multi-step suite covering: create, idempotency, bulk, PATCH versioning,
    # disallowed transition (409), allowed transition (history captures policy).
    # Returns 0 on success, non-zero on first failure.
    local base="http://localhost:${PORT_ARTEFACT}"
    local hdr_tenant='-H X-Tenant-ID:default'
    local hdr_json='-H Content-Type:application/json'
    local actor='urn:qa-aqa:user:smoke'
    local stamp="$(date +%s)-$$"

    # 1) create requirement
    local body
    body=$(curl -fs ${hdr_tenant} ${hdr_json} -H "Idempotency-Key: smoke-${stamp}" \
        -X POST "${base}/artefacts" \
        -d "{\"type\":\"requirement\",\"payload\":{\"title\":\"smoke\"},\"actor\":\"${actor}\"}") || return 1
    local aid
    aid=$(echo "${body}" | jq -r '.id')
    [[ -n "${aid}" && "${aid}" != "null" ]] || return 2

    # 2) same idempotency key → returns same id, no new row
    local body2
    body2=$(curl -fs ${hdr_tenant} ${hdr_json} -H "Idempotency-Key: smoke-${stamp}" \
        -X POST "${base}/artefacts" \
        -d "{\"type\":\"requirement\",\"payload\":{\"title\":\"smoke2\"},\"actor\":\"${actor}\"}") || return 3
    local aid2; aid2=$(echo "${body2}" | jq -r '.id')
    [[ "${aid}" == "${aid2}" ]] || return 4
    # Payload must be unchanged (the original)
    local title; title=$(echo "${body2}" | jq -r '.payload.title')
    [[ "${title}" == "smoke" ]] || return 5

    # 3) bulk create 3 items
    local bulk_body
    bulk_body=$(curl -fs ${hdr_tenant} ${hdr_json} -X POST "${base}/artefacts/bulk" \
        -d "{\"items\":[
            {\"type\":\"requirement\",\"payload\":{\"i\":1},\"actor\":\"${actor}\"},
            {\"type\":\"requirement\",\"payload\":{\"i\":2},\"actor\":\"${actor}\"},
            {\"type\":\"requirement\",\"payload\":{\"i\":3},\"actor\":\"${actor}\"}
        ]}") || return 6
    [[ $(echo "${bulk_body}" | jq 'length') == 3 ]] || return 7

    # 4) PATCH bumps version, writes history row
    curl -fs ${hdr_tenant} ${hdr_json} -X PATCH "${base}/artefacts/${aid}" \
        -d "{\"payload\":{\"title\":\"smoke patched\"},\"actor\":\"${actor}\"}" >/dev/null || return 8
    local version; version=$(curl -fs ${hdr_tenant} "${base}/artefacts/${aid}" | jq -r '.version')
    [[ "${version}" == "2" ]] || return 9
    local hist_count
    hist_count=$(curl -fs ${hdr_tenant} "${base}/artefacts/${aid}/history" | jq 'length')
    [[ "${hist_count}" == "1" ]] || return 10

    # 5) disallowed transition (draft→approved skips in_review) returns 409
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' ${hdr_tenant} ${hdr_json} \
        -X POST "${base}/artefacts/${aid}/transition" \
        -d "{\"to_state\":\"approved\",\"actor\":\"${actor}\"}")
    [[ "${code}" == "409" ]] || return 11

    # 6) allowed transition draft→in_review records policy_version in history
    curl -fs ${hdr_tenant} ${hdr_json} -X POST "${base}/artefacts/${aid}/transition" \
        -d "{\"to_state\":\"in_review\",\"actor\":\"${actor}\"}" >/dev/null || return 12
    local pol
    pol=$(curl -fs ${hdr_tenant} "${base}/artefacts/${aid}/history" \
        | jq -r '[.[] | select(.policy_version != null)] | last.policy_version')
    [[ "${pol}" == "default_v1" ]] || return 13

    return 0
}

_wait_for_log() {
    # Poll a container's logs for a regex, up to max_wait seconds. Returns 0 on match.
    # NB: We deliberately route logs through a temp file rather than a pipe.
    # `set -o pipefail` + early grep -q exit triggers SIGPIPE on podman logs,
    # which makes the pipeline return non-zero even when grep succeeded.
    local container="$1" pattern="$2" max_wait="${3:-120}"
    local tmpf
    tmpf=$(mktemp)
    podman logs "${container}" >"${tmpf}" 2>&1 || true
    if grep -qE "${pattern}" "${tmpf}"; then
        rm -f "${tmpf}"
        return 0
    fi
    local elapsed=0
    while (( elapsed < max_wait )); do
        podman logs --tail 500 "${container}" >"${tmpf}" 2>&1 || true
        if grep -qE "${pattern}" "${tmpf}"; then
            rm -f "${tmpf}"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    rm -f "${tmpf}"
    return 1
}

smoke_agent_workers_d1_suite() {
    # End-to-end D1 wiring test:
    #   - wait for the worker to be ready (Temporal connect + seed)
    #   - start a design-tests workflow via start_workflow.py (synchronous --wait)
    #   - assert the result contains test_case_ids
    #   - assert artefact-service has the test_case artefacts
    _wait_for_log agent-workers 'Worker started.*test-design' 180 || return 11
    local input='{"id":"R-smoke-001","title":"login happy and fail","acceptance_criteria":["valid creds return 200","invalid creds return 401"],"criticality":"low","tags":["auth"]}'
    local out
    out=$(podman exec agent-workers python -m src.start_workflow design-tests "${input}" default --wait 2>&1) || return 1
    # The CLI prints two lines: {"workflow_id":"..."} then {"workflow_id":"...","result":{...}}
    local result_line
    result_line=$(echo "${out}" | grep -E '"result"' | tail -1)
    [[ -n "${result_line}" ]] || return 2
    local test_case_count
    test_case_count=$(echo "${result_line}" | jq '.result.test_case_ids | length')
    [[ "${test_case_count}" =~ ^[0-9]+$ ]] && (( test_case_count >= 1 )) || return 3
    local wf_id
    wf_id=$(echo "${result_line}" | jq -r '.workflow_id')
    # Verify artefact-service sees the test_case rows
    local artefact_count
    artefact_count=$(curl -fs -H 'X-Tenant-ID: default' \
        "http://localhost:${PORT_ARTEFACT}/artefacts?type=test_case&workflow_id=${wf_id}" \
        | jq 'length')
    (( artefact_count == test_case_count )) || return 4
    return 0
}

smoke_executor_d1_1_suite() {
    # End-to-end D1.1 wiring test:
    #   1. Run design-tests to produce test_case artefacts (will use simple,
    #      browser-friendly test_case shape that maps to playwright keywords).
    #   2. Run execute-tests in mode=simulate; verify execution_result artefact present.
    #   3. Run execute-tests in mode=scripts language=playwright; verify .spec.ts content.
    #   4. Run execute-tests in mode=playwright_sandbox target_url=https://example.com;
    #      verify execution_result has at least 1 screenshot URL in MinIO.
    _wait_for_log agent-workers 'Worker started.*test-design' 180 || return 11

    # 1) Seed a deterministic test_case directly (faster than running design-tests)
    local tenant=default
    local stamp="$(date +%s)-$$"
    local tc_id="test_case:smoke-${stamp}"
    local tc_actor='urn:qa-aqa:user:smoke'
    local tc_body
    tc_body=$(cat <<JSON
{
  "id": "${tc_id}",
  "type": "test_case",
  "payload": {
    "title": "example.com loads",
    "steps": [
      {"library": "playwright", "keyword": "goto", "args": ["https://example.com"]},
      {"library": "playwright", "keyword": "expect_text", "args": ["h1", "Example Domain"]},
      {"library": "playwright", "keyword": "screenshot", "args": ["final"]}
    ],
    "expected_result": "h1 contains 'Example Domain'",
    "traceability_to_requirement": "smoke-req"
  },
  "actor": "${tc_actor}"
}
JSON
)
    curl -fs -H "X-Tenant-ID:${tenant}" -H 'Content-Type:application/json' \
        -X POST "http://localhost:${PORT_ARTEFACT}/artefacts" -d "${tc_body}" >/dev/null || return 1

    _run_execute_workflow() {
        local mode="$1"
        local extra="$2"
        local input
        input=$(cat <<JSON
{
  "test_case_ids": ["${tc_id}"],
  "mode": "${mode}"${extra}
}
JSON
)
        local out
        out=$(podman exec agent-workers python -m src.start_workflow execute-tests "${input}" default --wait 2>&1) || return 1
        echo "${out}" | grep -E '"result"' | tail -1
    }

    # 2) simulate
    local sim_line
    sim_line=$(_run_execute_workflow "simulate" "") || return 2
    local sim_count
    sim_count=$(echo "${sim_line}" | jq '.result.execution_result_ids | length')
    [[ "${sim_count}" == "1" ]] || return 3

    # 3) scripts (playwright)
    local sc_line
    sc_line=$(_run_execute_workflow "scripts" ',"language":"playwright"') || return 4
    local sc_id
    sc_id=$(echo "${sc_line}" | jq -r '.result.execution_result_ids[0]')
    local sc_payload
    sc_payload=$(curl -fs -H "X-Tenant-ID:${tenant}" "http://localhost:${PORT_ARTEFACT}/artefacts/${sc_id}" | jq -r '.payload.script_content')
    echo "${sc_payload}" | grep -qE 'page\.|playwright' || return 5

    # D1.2 — sandbox mode coverage moved to sandbox-cleanup smoke (deeper
    # checks + isolation). Chained suite covers simulate + scripts only.
    return 0
}

smoke_sandbox_cleanup() {
    # After any sandbox run, no qa-aqa-sandbox-* container should linger.
    # Run a quick sandbox workflow against example.com, then poll for cleanup.
    local tenant=default
    local stamp="cleanup-$(date +%s)-$$"
    local tc_id="test_case:${stamp}"
    local tc_actor='urn:qa-aqa:user:smoke'
    curl -fs -H "X-Tenant-ID:${tenant}" -H 'Content-Type:application/json' \
        -X POST "http://localhost:${PORT_ARTEFACT}/artefacts" -d "$(cat <<JSON
{"id":"${tc_id}","type":"test_case","payload":{"title":"cleanup","steps":[{"library":"playwright","keyword":"goto","args":["https://example.com"]},{"library":"playwright","keyword":"screenshot","args":["x"]}],"expected_result":"loaded","traceability_to_requirement":"smoke"},"actor":"${tc_actor}"}
JSON
)" >/dev/null || return 1
    local input='{"test_case_ids":["'"${tc_id}"'"],"mode":"playwright_sandbox","target_url":"https://example.com","sandbox_timeout_seconds":60,"allowed_urls":["https://example.com/*","http://example.com/*"]}'
    podman exec agent-workers python -m src.start_workflow execute-tests "${input}" default --wait >/dev/null 2>&1 || return 2
    # Poll for cleanup: must drop to zero within 30s
    local elapsed=0
    while (( elapsed < 30 )); do
        local count
        count=$(podman ps -a --filter "name=qa-aqa-sandbox-" --format '{{.Names}}' | grep -c .)
        (( count == 0 )) && return 0
        sleep 2; elapsed=$((elapsed + 2))
    done
    return 3
}

smoke_sandbox_isolation() {
    # A sandbox test_case that tries to reach an internal service MUST fail
    # — no DNS resolution for `artefact-service` from sandbox-egress network.
    local tenant=default
    local stamp="iso-$(date +%s)-$$"
    local tc_id="test_case:${stamp}"
    local tc_actor='urn:qa-aqa:user:smoke'
    curl -fs -H "X-Tenant-ID:${tenant}" -H 'Content-Type:application/json' \
        -X POST "http://localhost:${PORT_ARTEFACT}/artefacts" -d "$(cat <<JSON
{"id":"${tc_id}","type":"test_case","payload":{"title":"isolation probe","steps":[{"library":"playwright","keyword":"goto","args":["http://artefact-service:8003/health"]}],"expected_result":"must NOT be reachable","traceability_to_requirement":"smoke"},"actor":"${tc_actor}"}
JSON
)" >/dev/null || return 1
    # D1.3 — pass an allow-list that does NOT include artefact-service.
    # Sandbox should fail both because of proxy deny AND network isolation.
    local input='{"test_case_ids":["'"${tc_id}"'"],"mode":"playwright_sandbox","sandbox_timeout_seconds":30,"allowed_urls":["https://example.com/*"]}'
    local out
    out=$(podman exec agent-workers python -m src.start_workflow execute-tests "${input}" default --wait 2>&1) || return 2
    local er_id
    er_id=$(echo "${out}" | grep -E '"result"' | tail -1 | jq -r '.result.execution_result_ids[0]')
    [[ -n "${er_id}" && "${er_id}" != "null" ]] || return 3
    local payload
    payload=$(curl -fs -H "X-Tenant-ID:${tenant}" "http://localhost:${PORT_ARTEFACT}/artefacts/${er_id}" | jq -r '.payload')
    local status
    status=$(echo "${payload}" | jq -r '.status')
    # Must NOT be a pass — sandbox should fail to reach internal services
    [[ "${status}" == "fail" || "${status}" == "error" || "${status}" == "timeout" ]] || return 4
    return 0
}

smoke_proxy_d1_3_suite() {
    # D1.3 proves strict-default deny + allow path + deny path.
    local tenant=default
    local stamp="proxy-$(date +%s)-$$"
    local actor='urn:qa-aqa:user:smoke'
    _post_tc() {
        local id="$1" target_url="$2"
        curl -fs -H "X-Tenant-ID:${tenant}" -H 'Content-Type:application/json' \
            -X POST "http://localhost:${PORT_ARTEFACT}/artefacts" -d "$(cat <<JSON
{"id":"test_case:${id}","type":"test_case","payload":{"title":"proxy probe","steps":[{"library":"playwright","keyword":"goto","args":["${target_url}"]}],"expected_result":"goto","traceability_to_requirement":"smoke"},"actor":"${actor}"}
JSON
)" >/dev/null
    }
    _run_workflow() {
        local input="$1"
        local out
        out=$(podman exec agent-workers python -m src.start_workflow execute-tests "${input}" default --wait 2>&1) || return 1
        echo "${out}" | grep -E '"result"' | tail -1
    }
    _get_payload() {
        local id="$1"
        curl -fs -H "X-Tenant-ID:${tenant}" "http://localhost:${PORT_ARTEFACT}/artefacts/${id}" | jq -r '.payload'
    }

    # Test 1 — strict-default deny (no allowed_urls)
    _post_tc "${stamp}-strict" "https://example.com" || return 1
    local line1
    line1=$(_run_workflow '{"test_case_ids":["test_case:'${stamp}'-strict"],"mode":"playwright_sandbox","target_url":"https://example.com","sandbox_timeout_seconds":30}') || return 2
    local er1; er1=$(echo "${line1}" | jq -r '.result.execution_result_ids[0]')
    local p1; p1=$(_get_payload "${er1}")
    local status1; status1=$(echo "${p1}" | jq -r '.status')
    [[ "${status1}" != "pass" ]] || return 3   # should NOT pass

    # Test 2 — explicit allow for example.com → pass
    _post_tc "${stamp}-allow" "https://example.com" || return 4
    local line2
    line2=$(_run_workflow '{"test_case_ids":["test_case:'${stamp}'-allow"],"mode":"playwright_sandbox","target_url":"https://example.com","sandbox_timeout_seconds":60,"allowed_urls":["https://example.com/*","http://example.com/*"]}') || return 5
    local er2; er2=$(echo "${line2}" | jq -r '.result.execution_result_ids[0]')
    local p2; p2=$(_get_payload "${er2}")
    local status2; status2=$(echo "${p2}" | jq -r '.status')
    [[ "${status2}" == "pass" ]] || return 6

    # Test 3 — allow example.com but visit example.org → fail with denied
    _post_tc "${stamp}-deny" "https://example.org" || return 7
    local line3
    line3=$(_run_workflow '{"test_case_ids":["test_case:'${stamp}'-deny"],"mode":"playwright_sandbox","target_url":"https://example.org","sandbox_timeout_seconds":30,"allowed_urls":["https://example.com/*"]}') || return 8
    local er3; er3=$(echo "${line3}" | jq -r '.result.execution_result_ids[0]')
    local p3; p3=$(_get_payload "${er3}")
    local status3; status3=$(echo "${p3}" | jq -r '.status')
    [[ "${status3}" != "pass" ]] || return 9
    local err3; err3=$(echo "${p3}" | jq -r '.error_message // ""')
    # Either the proxy returned 403 or the sandbox couldn't TLS-handshake — both
    # acceptable as "denied".
    echo "${err3}" | grep -qE 'denied|403|net::' || return 10

    return 0
}

smoke_runner_d1_4_suite() {
    # D1.4 — async queue + per-tenant quotas via runner-service direct API.
    local base="http://localhost:${PORT_RUNNER}"
    local stamp="d14-$(date +%s)-$$"

    # 1) POST /runs returns 202 + run_id + status=queued
    local body
    body=$(cat <<JSON
{"test_case":{"payload":{"title":"d14 a","steps":[{"library":"playwright","keyword":"goto","args":["https://example.com"]},{"library":"playwright","keyword":"screenshot","args":["x"]}],"expected_result":"loaded","traceability_to_requirement":"smoke"}},"target_url":"https://example.com","timeout_seconds":60,"tenant_id":"default","workflow_id":"smoke:d14:${stamp}","test_case_id":"smoke-tc-1","allowed_urls":["https://example.com/*","http://example.com/*"]}
JSON
)
    local r1
    r1=$(curl -fs -o /tmp/d14_a.json -w '%{http_code}' -X POST "${base}/runs" \
        -H 'Content-Type: application/json' -d "${body}")
    [[ "${r1}" == "202" ]] || return 1
    local run_id; run_id=$(jq -r '.run_id' /tmp/d14_a.json)
    local status; status=$(jq -r '.status' /tmp/d14_a.json)
    [[ -n "${run_id}" && "${status}" == "queued" ]] || return 2

    # 2) Poll GET /runs/{id} until terminal
    local elapsed=0
    local final="queued"
    while (( elapsed < 240 )); do
        sleep 3; elapsed=$((elapsed + 3))
        final=$(curl -fs "${base}/runs/${run_id}" | jq -r '.status' || echo "?")
        [[ "${final}" == "completed" || "${final}" == "failed" ]] && break
    done
    [[ "${final}" == "completed" ]] || return 3

    # 3) Quota endpoint returns the expected shape
    local q; q=$(curl -fs "${base}/quota/default")
    echo "${q}" | jq -e '.concurrent.max == 3 and .daily.max == 100' >/dev/null || return 4

    return 0
}

smoke_runner_d1_4_quota_concurrent() {
    # Submit 4 runs rapid-fire; the 4th MUST be 429 with kind=quota_concurrent.
    # Use a deliberately long timeout so the first 3 stay running while we
    # submit the 4th.
    local base="http://localhost:${PORT_RUNNER}"
    local stamp="d14q-$(date +%s)-$$"
    local tenant="quota-test-${stamp}"  # fresh tenant so we don't tangle with other smokes
    # Body factory — long timeout, but the runs themselves won't even start
    # in time because the queue worker processes serially.
    _post() {
        local i="$1"
        local body
        body=$(cat <<JSON
{"test_case":{"payload":{"title":"q ${i}","steps":[{"library":"playwright","keyword":"goto","args":["https://example.com"]},{"library":"playwright","keyword":"screenshot","args":["x"]}],"expected_result":"loaded","traceability_to_requirement":"smoke"}},"target_url":"https://example.com","timeout_seconds":300,"tenant_id":"${tenant}","workflow_id":"smoke:q:${stamp}:${i}","test_case_id":"smoke-tc-${i}","allowed_urls":["https://example.com/*"]}
JSON
)
        curl -s -o /tmp/d14q_${i}.json -w '%{http_code}' -X POST "${base}/runs" \
            -H 'Content-Type: application/json' -d "${body}"
    }
    local c1 c2 c3 c4
    c1=$(_post 1); c2=$(_post 2); c3=$(_post 3); c4=$(_post 4)
    [[ "${c1}" == "202" && "${c2}" == "202" && "${c3}" == "202" ]] || return 1
    [[ "${c4}" == "429" ]] || return 2
    # Body of 4th must indicate quota_concurrent
    jq -e '.detail.kind == "quota_concurrent"' /tmp/d14q_4.json >/dev/null || return 3
    return 0
}

smoke_runner_cancel_queued() {
  # Concurrent slot is reserved at submit (D1.4 semantics). Concurrent cap is 3,
  # so we submit 2 long-running playwright fillers to occupy the worker, then a
  # 3rd "target" that lands behind them in runs:queue. Cancel the target while
  # it is still queued.
  local TENANT="smk-cancel-q-$RANDOM"
  local _post_run
  _post_run() {
    local role="$1"
    curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' -d "$(cat <<JSON
{"tenant_id":"$TENANT","workflow_id":"smoke:cancel-q","test_case_id":"$role",
 "test_case":{"payload":{"title":"$role","steps":[
   {"library":"playwright","keyword":"goto","args":["https://example.com"]},
   {"library":"playwright","keyword":"screenshot","args":["x"]}],
   "expected_result":"loaded","traceability_to_requirement":"smoke"}},
 "target_url":"https://example.com","timeout_seconds":300,
 "allowed_urls":["https://example.com/*"]}
JSON
)" | jq -r .run_id
  }
  local f1 f2 target
  f1=$(_post_run filler-1) || return 1
  [[ -n "$f1" && "$f1" != "null" ]] || { echo "filler-1 submit failed: rid='$f1'"; return 1; }
  f2=$(_post_run filler-2) || return 1
  [[ -n "$f2" && "$f2" != "null" ]] || { echo "filler-2 submit failed: rid='$f2'"; return 1; }
  target=$(_post_run target) || return 1
  [[ -n "$target" && "$target" != "null" ]] || { echo "target submit failed: rid='$target'"; return 1; }
  # Target must currently be queued (worker is busy with filler-1's sandbox).
  local tst
  tst=$(curl -fsS "http://localhost:8004/runs/$target" | jq -r .status)
  [[ "$tst" == "queued" ]] || { echo "target not queued, status=$tst"; return 1; }
  # Cancel the queued target.
  local code
  code=$(curl -s -o /tmp/cancel_q.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$target/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:smoke\",\"tenant_id\":\"$TENANT\"}")
  [[ "$code" == "200" ]] || { echo "expected 200, got $code body=$(cat /tmp/cancel_q.out)"; return 1; }
  # Status must be canceled.
  local status
  status=$(curl -fsS "http://localhost:8004/runs/$target" | jq -r .status)
  [[ "$status" == "canceled" ]] || { echo "expected status=canceled, got $status"; return 1; }
  # tenant:running must NOT contain the canceled target (slot released).
  local in_set
  in_set=$(podman exec valkey valkey-cli SISMEMBER "tenant:$TENANT:running" "$target")
  [[ "$in_set" == "0" ]] || { echo "tenant:running still contains $target"; return 1; }
}

smoke_runner_cancel_cross_tenant() {
  # Tenant A holds a long-running filler that occupies the worker; tenant A
  # submits a target that queues behind the filler; tenant B attempts to cancel
  # the target -> 403. We deliberately verify while the target is queued so the
  # tenant check in cancel_queued Lua fires (the endpoint's terminal/running
  # dispatch branches don't currently tenant-check -- tracked separately).
  local TA="smk-xtA-$RANDOM"
  local TB="smk-xtB-$RANDOM"
  local _post_run
  _post_run() {
    local tenant="$1" role="$2"
    curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' -d "$(cat <<JSON
{"tenant_id":"$tenant","workflow_id":"smoke:cancel-xt","test_case_id":"$role",
 "test_case":{"payload":{"title":"$role","steps":[
   {"library":"playwright","keyword":"goto","args":["https://example.com"]},
   {"library":"playwright","keyword":"screenshot","args":["x"]}],
   "expected_result":"loaded","traceability_to_requirement":"smoke"}},
 "target_url":"https://example.com","timeout_seconds":300,
 "allowed_urls":["https://example.com/*"]}
JSON
)" | jq -r .run_id
  }
  local fill tgt
  fill=$(_post_run "$TA" filler) || return 1
  [[ -n "$fill" && "$fill" != "null" ]] || { echo "filler submit failed: rid='$fill'"; return 1; }
  tgt=$(_post_run "$TA" target) || return 1
  [[ -n "$tgt" && "$tgt" != "null" ]] || { echo "target submit failed: rid='$tgt'"; return 1; }
  local st
  st=$(curl -fsS "http://localhost:8004/runs/$tgt" | jq -r .status)
  [[ "$st" == "queued" ]] || { echo "target not queued, status=$st"; return 1; }
  local code
  code=$(curl -s -o /tmp/cancel_xt.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$tgt/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:b\",\"tenant_id\":\"$TB\"}")
  [[ "$code" == "403" ]] || { echo "expected 403 cross-tenant, got $code body=$(cat /tmp/cancel_xt.out)"; return 1; }
  # Cleanup: cancel target as the owning tenant so its slot frees.
  curl -s -X POST "http://localhost:8004/runs/$tgt/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:a\",\"tenant_id\":\"$TA\"}" > /dev/null
}

smoke_runner_cancel_already_terminal() {
  # Submit a real playwright run and wait for it to terminate cleanly. Then
  # cancel -> 409 (terminal). The plan's original "steps:[]" payload is
  # unreliable: with no steps the executor may error out (failed) or complete
  # instantly, races against the poll loop. Using goto+screenshot mirrors the
  # payload other smokes rely on.
  local TENANT="smk-term-$RANDOM"
  local rid
  rid=$(curl -fsS -X POST http://localhost:8004/runs -H 'Content-Type: application/json' -d "$(cat <<JSON
{"tenant_id":"$TENANT","workflow_id":"smoke:cancel-term","test_case_id":"term",
 "test_case":{"payload":{"title":"term","steps":[
   {"library":"playwright","keyword":"goto","args":["https://example.com"]},
   {"library":"playwright","keyword":"screenshot","args":["x"]}],
   "expected_result":"loaded","traceability_to_requirement":"smoke"}},
 "target_url":"https://example.com","timeout_seconds":120,
 "allowed_urls":["https://example.com/*"]}
JSON
)" | jq -r .run_id) || return 1
  [[ -n "$rid" && "$rid" != "null" ]] || { echo "submit failed: rid='$rid'"; return 1; }
  local status="" i
  for i in $(seq 1 120); do
    status=$(curl -fsS "http://localhost:8004/runs/$rid" | jq -r .status)
    [[ "$status" == "completed" || "$status" == "failed" ]] && break
    sleep 1
  done
  [[ "$status" == "completed" || "$status" == "failed" ]] || { echo "run did not terminate in 120s, last status=$status"; return 1; }
  local code
  code=$(curl -s -o /tmp/cancel_term.out -w '%{http_code}' \
    -X POST "http://localhost:8004/runs/$rid/cancel" \
    -H 'Content-Type: application/json' \
    -d "{\"actor_urn\":\"urn:qa-aqa:user:smoke\",\"tenant_id\":\"$TENANT\"}")
  [[ "$code" == "409" ]] || { echo "expected 409, got $code body=$(cat /tmp/cancel_term.out)"; return 1; }
}

smoke_bff_quota_in_me() {
    # BFF /api/me includes quota.concurrent + quota.daily for the caller's tenant.
    local base="http://localhost:${PORT_APISIX_PROXY}/api"
    local login_body
    login_body=$(curl -fs -X POST -H 'Content-Type: application/json' "${base}/auth/login" \
        -d '{"email":"reviewer@qa-aqa.local","password":"reviewer123"}') || return 1
    local token; token=$(echo "${login_body}" | jq -r '.access_token')
    local me; me=$(curl -fs -H "Authorization: Bearer ${token}" "${base}/me")
    echo "${me}" | jq -e '.quota.concurrent.max == 3 and .quota.daily.max == 100' >/dev/null || return 2
    return 0
}

smoke_bff_d3c_suite() {
    # D3c — execute-tests via BFF + media proxy.
    local base="http://localhost:${PORT_APISIX_PROXY}/api"
    local login_body
    login_body=$(curl -fs -X POST -H 'Content-Type: application/json' "${base}/auth/login" \
        -d '{"email":"reviewer@qa-aqa.local","password":"reviewer123"}') || return 1
    local token; token=$(echo "${login_body}" | jq -r '.access_token')

    # 1) Seed a test_case directly
    local stamp; stamp="d3c-$(date +%s)-$$"
    local tc_id="test_case:${stamp}"
    curl -fs -H 'X-Tenant-ID: default' -H 'Content-Type: application/json' \
        -X POST "http://localhost:${PORT_ARTEFACT}/artefacts" \
        -d "{\"id\":\"${tc_id}\",\"type\":\"test_case\",\"payload\":{\"title\":\"d3c\",\"steps\":[{\"library\":\"playwright\",\"keyword\":\"goto\",\"args\":[\"https://example.com\"]},{\"library\":\"playwright\",\"keyword\":\"screenshot\",\"args\":[\"x\"]}],\"expected_result\":\"loaded\",\"traceability_to_requirement\":\"smoke\"},\"actor\":\"urn:qa-aqa:user:smoke\"}" >/dev/null || return 2

    # 2) Start execute-tests via BFF (sandbox mode)
    local body
    body=$(cat <<JSON
{"test_case_ids":["${tc_id}"],"mode":"playwright_sandbox","target_url":"https://example.com","sandbox_timeout_seconds":60,"allowed_urls":["https://example.com/*","http://example.com/*"]}
JSON
)
    local start_resp
    start_resp=$(curl -fs -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -X POST "${base}/workflows/execute-tests" -d "${body}") || return 3
    local wf_id; wf_id=$(echo "${start_resp}" | jq -r '.workflow_id')
    [[ "${wf_id}" == default:execute-tests:* ]] || return 4

    # 3) Poll status until COMPLETED (max 5 min — sandbox cold start)
    local elapsed=0 status="RUNNING"
    while (( elapsed < 300 )); do
        local sresp
        sresp=$(curl -fs -H "Authorization: Bearer ${token}" \
            "${base}/workflow-status/$(printf %s "${wf_id}" | jq -sRr @uri)") || return 5
        status=$(echo "${sresp}" | jq -r '.status')
        [[ "${status}" == "COMPLETED" || "${status}" == "FAILED" ]] && break
        sleep 5; elapsed=$((elapsed + 5))
    done
    [[ "${status}" == "COMPLETED" ]] || return 6

    # 4) Verify execution_result has screenshots[]
    local wf_detail
    wf_detail=$(curl -fs -H "Authorization: Bearer ${token}" \
        "${base}/workflows/$(printf %s "${wf_id}" | jq -sRr @uri)")
    local first_er_screenshots
    first_er_screenshots=$(echo "${wf_detail}" | jq -r '.artefacts_by_type.execution_result[0].payload.screenshots // []')
    local first_screenshot
    first_screenshot=$(echo "${first_er_screenshots}" | jq -r '.[0] // empty')
    [[ -n "${first_screenshot}" ]] || return 7

    # 5) Strip s3://bucket/ prefix to get the key, then fetch via BFF media proxy
    local key; key=$(echo "${first_screenshot}" | sed 's|^s3://[^/]*/||')
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${token}" \
        "${base}/media?key=$(printf %s "${key}" | jq -sRr @uri)")
    [[ "${code}" == "200" ]] || return 8

    # 6) Cross-tenant scope check: key under another tenant should 403
    local cross
    cross=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${token}" \
        "${base}/media?key=$(printf %s "executions/another-tenant/foo.png" | jq -sRr @uri)")
    [[ "${cross}" == "403" ]] || return 9

    return 0
}

smoke_bff_d3b_suite() {
    # D3b end-to-end: start design-tests via BFF -> poll status -> verify test_cases.
    local base="http://localhost:${PORT_APISIX_PROXY}/api"
    local login_body
    login_body=$(curl -fs -X POST -H 'Content-Type: application/json' "${base}/auth/login" \
        -d '{"email":"reviewer@qa-aqa.local","password":"reviewer123"}') || return 1
    local token; token=$(echo "${login_body}" | jq -r '.access_token')

    local stamp; stamp=$(date +%s)
    local body
    body=$(cat <<JSON
{"requirement":{"id":"R-d3b-${stamp}","title":"d3b login","acceptance_criteria":["valid creds return 200","invalid creds return 401"]},"criticality":"low"}
JSON
)
    local start_resp
    start_resp=$(curl -fs -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -X POST "${base}/workflows/design-tests" -d "${body}") || return 2
    local wf_id; wf_id=$(echo "${start_resp}" | jq -r '.workflow_id')
    [[ "${wf_id}" == default:design-tests:* ]] || return 3

    # Poll status until terminal (max 10 min — LLM cold start can be slow)
    local elapsed=0
    local status="RUNNING"
    while (( elapsed < 600 )); do
        local sresp
        sresp=$(curl -fs -H "Authorization: Bearer ${token}" \
            "${base}/workflow-status/$(printf %s "${wf_id}" | jq -sRr @uri)") || return 4
        status=$(echo "${sresp}" | jq -r '.status')
        [[ "${status}" == "COMPLETED" || "${status}" == "FAILED" ]] && break
        sleep 5; elapsed=$((elapsed + 5))
    done
    [[ "${status}" == "COMPLETED" ]] || return 5

    # Verify test_cases via /api/workflows/{id}
    local wf_detail
    wf_detail=$(curl -fs -H "Authorization: Bearer ${token}" \
        "${base}/workflows/$(printf %s "${wf_id}" | jq -sRr @uri)")
    local tc_count
    tc_count=$(echo "${wf_detail}" | jq '.artefacts_by_type.test_case | length // 0')
    (( tc_count >= 1 )) || return 6
    local parent
    parent=$(echo "${wf_detail}" | jq -r '.artefacts_by_type.test_case[0].parent_id')
    echo "${parent}" | grep -qE '^requirement:' || return 7
    return 0
}

smoke_bff_d3a_suite() {
    # D3a end-to-end: login -> list workflows -> transition an artefact.
    local base="http://localhost:${PORT_APISIX_PROXY}/api"
    # 1) login as reviewer
    local login_body
    login_body=$(curl -fs -X POST -H 'Content-Type: application/json' "${base}/auth/login" \
        -d '{"email":"reviewer@qa-aqa.local","password":"reviewer123"}') || return 1
    local token; token=$(echo "${login_body}" | jq -r '.access_token')
    [[ -n "${token}" && "${token}" != "null" ]] || return 2
    local role; role=$(echo "${login_body}" | jq -r '.user.role')
    [[ "${role}" == "reviewer" ]] || return 3

    # 2) /api/me reflects token
    local me; me=$(curl -fs -H "Authorization: Bearer ${token}" "${base}/me" | jq -r '.email')
    [[ "${me}" == "reviewer@qa-aqa.local" ]] || return 4

    # 3) Create a fresh artefact directly via artefact-service so we have something to transition.
    local stamp="d3a-$(date +%s)-$$"
    local aid="test_case:${stamp}"
    curl -fs -H 'X-Tenant-ID: default' -H 'Content-Type: application/json' \
        -X POST "http://localhost:${PORT_ARTEFACT}/artefacts" \
        -d "{\"id\":\"${aid}\",\"type\":\"test_case\",\"payload\":{\"title\":\"d3a smoke\",\"steps\":[],\"expected_result\":\"-\",\"traceability_to_requirement\":\"-\"},\"actor\":\"urn:qa-aqa:user:smoke\",\"workflow_id\":\"default:d3a-smoke:${stamp}\"}" >/dev/null || return 5

    # 4) Transition via BFF (which stamps the actor from the JWT)
    local trans; trans=$(curl -fs -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -X POST "${base}/artefacts/${aid}/transition" -d '{"to_state":"in_review"}')
    local new_state; new_state=$(echo "${trans}" | jq -r '.state')
    [[ "${new_state}" == "in_review" ]] || return 6

    # 5) History shows the transition with the user's URN as actor
    local hist; hist=$(curl -fs -H "Authorization: Bearer ${token}" "${base}/artefacts/${aid}/history")
    local actor; actor=$(echo "${hist}" | jq -r '[.[] | select(.policy_version != null)] | last.actor')
    [[ "${actor}" == "urn:qa-aqa:user:reviewer" ]] || return 7

    # 6) Workflow appears in /api/workflows list
    local wfs; wfs=$(curl -fs -H "Authorization: Bearer ${token}" "${base}/workflows")
    echo "${wfs}" | jq -e --arg id "default:d3a-smoke:${stamp}" '[.workflows[] | select(.workflow_id == $id)] | length == 1' >/dev/null || return 8

    return 0
}

smoke_tests() {
    step "7/8  Smoke tests"
    # shellcheck disable=SC1091
    source "${ENV_FILE}"
    local failures=0
    local tests=(
        "postgres|PGPASSWORD='${POSTGRES_PASSWORD}' psql -h 127.0.0.1 -p ${PORT_POSTGRES} -U '${POSTGRES_USER}' -d '${POSTGRES_DB}' -tAc \"SELECT extname FROM pg_extension WHERE extname='vector';\" | grep -q vector"
        "opensearch|curl -fs http://localhost:${PORT_OPENSEARCH}/_cluster/health | jq -e '.status==\"green\" or .status==\"yellow\"' >/dev/null"
        "neo4j|curl -fs -o /dev/null -w '%{http_code}' http://localhost:${PORT_NEO4J_HTTP} | grep -q 200"
        "valkey|(echo PING; sleep 0.2) | nc -w 2 localhost ${PORT_VALKEY} | grep -q PONG"
        "temporal-ui|curl -fs -o /dev/null -w '%{http_code}' http://localhost:${PORT_TEMPORAL_UI} | grep -q 200"
        "prometheus|curl -fs http://localhost:${PORT_PROMETHEUS}/-/healthy"
        "grafana|curl -fs http://localhost:${PORT_GRAFANA}/api/health | jq -e '.database==\"ok\"' >/dev/null"
        "loki|curl -fs http://localhost:${PORT_LOKI}/ready"
        "otel-collector|curl -fs http://localhost:${PORT_OTEL_HEALTH}/ >/dev/null"
        "ollama|curl -fs http://localhost:${PORT_OLLAMA}/api/version | jq -e '.version' >/dev/null"
        "model-gateway-liveness|curl -fs http://localhost:${PORT_LITELLM}/health/liveliness >/dev/null"
        "model-gateway-embedding|curl -fs -X POST http://localhost:${PORT_LITELLM}/v1/embeddings -H 'Authorization: Bearer ${LITELLM_MASTER_KEY}' -H 'Content-Type: application/json' -d '{\"model\":\"embed-dev\",\"input\":\"hello\"}' | jq -e '.data[0].embedding | length == 768' >/dev/null"
        "opa|curl -fs http://localhost:${PORT_OPA}/health >/dev/null"
        "rag-service-health|curl -fs http://localhost:${PORT_RAG}/health | jq -e '.status==\"ok\"' >/dev/null"
        "rag-service-ingest|curl -fs -X POST http://localhost:${PORT_RAG}/ingest -H 'Content-Type: application/json' -d '{\"id\":\"smoke-doc-1\",\"text\":\"# Greetings\\n\\nHello world this is a smoke test document about valkey and postgres.\",\"metadata\":{\"src\":\"smoke\"}}' | jq -e '.chunks > 0' >/dev/null"
        "rag-service-search|curl -fs -X POST http://localhost:${PORT_RAG}/search -H 'Content-Type: application/json' -d '{\"query\":\"hello postgres\",\"k\":3}' | jq -e '.hits | length > 0' >/dev/null"
        "policy-svc-health|curl -fs http://localhost:${PORT_POLICY}/health | jq -e '.status==\"ok\"' >/dev/null"
        "policy-svc-allow-admin|curl -fs -X POST http://localhost:${PORT_POLICY}/authorize -H 'Content-Type: application/json' -d '{\"subject\":{\"role\":\"admin\"},\"action\":\"delete\",\"resource\":{\"id\":\"x\"}}' | jq -e '.allow==true' >/dev/null"
        "policy-svc-deny-default|curl -fs -X POST http://localhost:${PORT_POLICY}/authorize -H 'Content-Type: application/json' -d '{\"subject\":{\"role\":\"viewer\"},\"action\":\"delete\",\"resource\":{\"visibility\":\"private\"}}' | jq -e '.allow==false' >/dev/null"
        "artefact-health|curl -fs http://localhost:${PORT_ARTEFACT}/health | jq -e '.status==\"ok\"' >/dev/null"
        "artefact-rls-gate|[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:${PORT_ARTEFACT}/artefacts -H 'Content-Type: application/json' -d '{\"type\":\"requirement\",\"payload\":{},\"actor\":\"urn:qa-aqa:user:smoke\"}') = '400' ]"
        "artefact-urn-422|[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:${PORT_ARTEFACT}/artefacts -H 'Content-Type: application/json' -H 'X-Tenant-ID: default' -d '{\"type\":\"requirement\",\"payload\":{},\"actor\":\"alice\"}') = '422' ]"
        "artefact-seed-loaded|curl -fs -H 'X-Tenant-ID: default' 'http://localhost:${PORT_ARTEFACT}/artefacts?type=approval_policy' | jq -e 'length >= 1' >/dev/null"
        "artefact-default-policy-fallback|curl -fs -H 'X-Tenant-ID: default' 'http://localhost:${PORT_ARTEFACT}/policies/approval/test_case' | jq -e '.name==\"default_v1\"' >/dev/null"
        "artefact-d05-suite|smoke_artefact_d05_suite"
        "agent-workers-booted|_wait_for_log agent-workers 'Worker started.*test-design' 180"
        "agent-workers-seed-ran|_wait_for_log agent-workers 'Seed: [0-9]+ spec docs ingested' 180"
        "agent-workers-d1-suite|smoke_agent_workers_d1_suite"
        "minio-health|curl -fs http://localhost:${PORT_MINIO_API}/minio/health/live >/dev/null"
        "runner-service-health|curl -fs http://localhost:${PORT_RUNNER}/health | jq -e '.status==\"ok\"' >/dev/null"
        "executor-d1-1-suite|smoke_executor_d1_1_suite"
        "sandbox-network-exists|podman network ls --format '{{.Name}}' | grep -qE '^sandbox-egress\$'"
        "sandbox-no-cross-network|! podman network inspect sandbox-egress 2>/dev/null | jq -r '.[0].containers // {} | keys[]?' | xargs -I{} podman inspect {} --format '{{.NetworkSettings.Networks}}' 2>/dev/null | grep -q qa-aqa"
        "sandbox-cleanup|smoke_sandbox_cleanup"
        "sandbox-isolation|smoke_sandbox_isolation"
        "proxy-tcp-up|bash -c '</dev/tcp/127.0.0.1/${PORT_PROXY_ADMIN}' 2>&1 | grep -qE '^$' || true; (echo > /dev/tcp/127.0.0.1/${PORT_PROXY_ADMIN}) 2>/dev/null"
        "proxy-d1-3-suite|smoke_proxy_d1_3_suite"
        "bff-health|curl -fs http://localhost:${PORT_BFF}/api/health | jq -e '.status==\"ok\"' >/dev/null"
        "apisix-401-on-api|[ \$(curl -s -o /dev/null -w '%{http_code}' http://localhost:${PORT_APISIX_PROXY}/api/me) = '401' ]"
        "apisix-serves-web|curl -fs http://localhost:${PORT_APISIX_PROXY}/ | grep -qE '<title>QA/AQA</title>'"
        "bff-d3a-suite|smoke_bff_d3a_suite"
        "bff-designer-needs-role|[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:${PORT_APISIX_PROXY}/api/workflows/design-tests) = '401' ]"
        "bff-d3b-suite|smoke_bff_d3b_suite"
        "bff-media-401|[ \$(curl -s -o /dev/null -w '%{http_code}' http://localhost:${PORT_APISIX_PROXY}/api/media?key=executions/default/x) = '401' ]"
        "bff-d3c-suite|smoke_bff_d3c_suite"
        "runner-d1-4-suite|smoke_runner_d1_4_suite"
        "runner-quota-concurrent|smoke_runner_d1_4_quota_concurrent"
        "bff-quota-in-me|smoke_bff_quota_in_me"
        "runner-cancel-queued|smoke_runner_cancel_queued"
        "runner-cancel-cross-tenant|smoke_runner_cancel_cross_tenant"
        "runner-cancel-already-terminal|smoke_runner_cancel_already_terminal"
    )
    for t in "${tests[@]}"; do
        local name="${t%%|*}"
        local cmd="${t#*|}"
        if eval "${cmd}" >/dev/null 2>&1; then
            ok "smoke: ${name}"
        else
            err "smoke: ${name} FAILED  → ${cmd}"
            failures=$((failures+1))
        fi
    done
    step "8/8  Summary"
    if (( failures == 0 )); then
        ok "All smoke tests passed."
        echo
        echo "Endpoints:"
        echo "  Grafana       http://localhost:${PORT_GRAFANA}        admin / ${GRAFANA_ADMIN_PASSWORD}"
        echo "  Prometheus    http://localhost:${PORT_PROMETHEUS}"
        echo "  Loki          http://localhost:${PORT_LOKI}"
        echo "  Temporal UI   http://localhost:${PORT_TEMPORAL_UI}"
        echo "  Neo4j         http://localhost:${PORT_NEO4J_HTTP}    neo4j / ${NEO4J_PASSWORD}"
        echo "  OpenSearch    http://localhost:${PORT_OPENSEARCH}"
        echo "  Postgres      localhost:${PORT_POSTGRES}             ${POSTGRES_USER} / ${POSTGRES_PASSWORD}"
        echo "  Valkey        localhost:${PORT_VALKEY}"
        echo "  OTel OTLP     localhost:${PORT_OTEL_GRPC} (gRPC) / ${PORT_OTEL_HTTP} (HTTP)"
        echo "  Ollama        http://localhost:${PORT_OLLAMA}"
        echo "  model-gateway http://localhost:${PORT_LITELLM}     Bearer \${LITELLM_MASTER_KEY}"
        echo "  OPA           http://localhost:${PORT_OPA}/health"
        echo "  rag-service   http://localhost:${PORT_RAG}     POST /ingest, /search"
        echo "  policy-svc    http://localhost:${PORT_POLICY}     POST /authorize"
        echo "  artefact-svc  http://localhost:${PORT_ARTEFACT}     POST /artefacts (X-Tenant-ID required)"
        echo "  runner-svc    http://localhost:${PORT_RUNNER}     POST /runs"
        echo "  MinIO web     http://localhost:${PORT_MINIO_WEB}     \${MINIO_ROOT_USER} / \${MINIO_ROOT_PASSWORD}"
        echo "  MinIO S3 API  http://localhost:${PORT_MINIO_API}"
        echo "  APISIX        http://localhost:${PORT_APISIX_PROXY}      front door"
        echo "  APISIX admin  http://localhost:${PORT_APISIX_ADMIN}      X-API-KEY: ${APISIX_ADMIN_KEY}"
        echo "  bff           http://localhost:${PORT_BFF}/api/health    direct port (CORS-restricted via APISIX in prod)"
        echo "  Web UI        http://localhost:${PORT_APISIX_PROXY}/login admin@qa-aqa.local / admin123"
        return 0
    else
        err "${failures} smoke test(s) failed"
        return 1
    fi
}

status() {
    step "Status"
    podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
}

# =============================================================================
# Main
# =============================================================================

usage() {
    sed -n '2,16p' "$0"
}

cmd="${1:-up}"
case "${cmd}" in
    dry-run)
        DRY_RUN=1
        ensure_brew_packages
        ensure_podman_machine
        write_dist_dir
        ensure_env_file
        bring_up
        ;;
    up)
        ensure_brew_packages
        ensure_podman_machine
        write_dist_dir
        ensure_env_file
        build_custom_images
        bring_up
        wait_healthy
        ollama_preload
        smoke_tests
        ;;
    down)
        bring_down
        ;;
    nuke)
        nuke
        ;;
    status)
        status
        ;;
    smoke)
        smoke_tests
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        err "Unknown command: ${cmd}"
        usage
        exit 2
        ;;
esac
