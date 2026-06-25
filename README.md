# QA / AQA

Infrastructure for the QA/AQA agentic platform. One bootstrap script, open-source components only, portable across dev → server → k8s.

## Quick start

```sh
./infra/infra.sh up
```

Brings up a 10-service data + observability stack on Podman:

| Service | URL / port |
|---|---|
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |
| Loki | http://localhost:3100 |
| Temporal UI | http://localhost:8233 |
| Neo4j | http://localhost:7474 (neo4j / neo4jdevpw) |
| OpenSearch | http://localhost:9200 |
| Postgres + pgvector | localhost:55432 (app / appdevpw) |
| Valkey | localhost:6379 |
| OTel OTLP | localhost:4317 (gRPC), 4318 (HTTP) |

## Other commands

```sh
./infra/infra.sh smoke    # re-run wiring tests
./infra/infra.sh status   # container table
./infra/infra.sh down     # stop, preserve volumes
./infra/infra.sh nuke     # stop + wipe volumes (destructive)
./infra/infra.sh dry-run  # print plan, no changes
```

## Editing the stack

`infra/infra.sh` is the single source of truth. Edit it, rerun `./infra/infra.sh up` — idempotent. Generated `dist/` files are gitignored.

## Specs

Sub-project design docs: [`docs/superpowers/specs/`](docs/superpowers/specs/).

## Sub-projects

| ID | Scope | Status |
|---|---|---|
| 0 | Podman foundation | ✅ shipped (in `infra.sh`) |
| A | Data + control plane (Postgres+pgvector, OpenSearch, Neo4j, Valkey, Temporal) | ✅ shipped |
| B | Observability (OTel, Prometheus, Grafana, Loki) | ✅ shipped |
| C | Model gateway + RAG + policy | next |
| D | Agent workers + connectors + edge + web | planned |
