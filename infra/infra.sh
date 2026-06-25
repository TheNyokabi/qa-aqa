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
readonly INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DIST_DIR="${INFRA_DIR}/dist"
readonly ENV_FILE="${INFRA_DIR}/.env"

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
        "${DIST_DIR}/opa/policies/qa_aqa"

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

volumes:
  pg-data:
  os-data:
  neo4j-data:
  valkey-data:
  prom-data:
  grafana-data:
  loki-data:
  ollama-data:

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
    step "5a/8  Build custom images (rag-service, policy-svc)"
    local services=("rag" "policy")
    local images=("${IMG_RAG}" "${IMG_POLICY}")
    for i in "${!services[@]}"; do
        local svc="${services[$i]}"
        local img="${images[$i]}"
        local ctx="${INFRA_DIR%/infra}/services/${svc}"
        if [[ ! -d "${ctx}" ]]; then
            warn "no source at ${ctx} — skipping ${img}"
            continue
        fi
        log "podman build ${img}"
        run_or_print "podman build -t '${img}' -f '${ctx}/Containerfile' '${ctx}'"
    done
}

bring_up() {
    step "5/8  Pull images + start stack"
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
    local services=(postgres opensearch neo4j valkey prometheus grafana loki otel-collector temporal temporal-ui ollama model-gateway opa rag-service policy-svc)
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
