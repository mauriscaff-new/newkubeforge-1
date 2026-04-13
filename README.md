# KubeForge

[![CI](https://github.com/mauriscaff/kubeforge/actions/workflows/ci.yml/badge.svg)](https://github.com/mauriscaff/kubeforge/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-87%25-brightgreen)](#testes-e-qualidade)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fmauriscaff%2Fkubeforge-blue)](https://ghcr.io/mauriscaff/kubeforge)

KubeForge e uma API FastAPI que analisa um projeto de software e gera automaticamente os artefatos necessarios para containerizacao e deploy em Kubernetes: Dockerfile, `.dockerignore`, manifests K8s e scripts de operacao.

## Instalacao

### Com Docker (recomendado para ambiente local completo)

```bash
docker compose up --build
```

API: `http://localhost:8000`  
Redis: `localhost:6379`

### Sem Docker

Requisitos:
- Python 3.12+
- `uv`

```bash
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Como usar a API (curl)

### GET /

```bash
curl http://localhost:8000/
```

### GET /health

```bash
curl http://localhost:8000/health
```

### POST /analyze

Exemplo com upload de ZIP:

```bash
curl -X POST http://localhost:8000/analyze \
  -F "source_type=zip" \
  -F "file=@./meu-projeto.zip"
```

Exemplo com Git:

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"source_type":"git","git_url":"https://github.com/seu-org/seu-repo.git"}'
```

### POST /generate

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "session_id":"SEU_SESSION_ID",
    "app_name":"meu-app",
    "image":"ghcr.io/seu-org/meu-app:latest",
    "namespace":"default",
    "service_type":"ClusterIP",
    "replicas":2,
    "port":8000,
    "enable_hpa":true,
    "enable_network_policy":true,
    "env":{"LOG_LEVEL":"info","API_KEY":"placeholder"}
  }'
```

### POST /download

```bash
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"session_id":"SEU_SESSION_ID"}' \
  --output kubeforge-output.zip
```

## Linguagens e Frameworks Suportados

| Linguagem | Frameworks detectados | Build tool detectado |
| --- | --- | --- |
| Node.js | `nextjs`, `nestjs`, `express`, `fastify`, `node` | `npm`, `yarn`, `pnpm` |
| Python | `fastapi`, `django`, `flask`, `python` | `uv`, `poetry`, `pip` |
| Java | `spring` (Maven/Gradle), `java` | `maven`, `gradle` |
| Go | `go` | `go` |
| .NET | `dotnet` | `dotnet` |

## Arquivos Gerados

| Arquivo | Descricao |
| --- | --- |
| `Dockerfile` | Dockerfile renderizado pelo template da linguagem detectada. |
| `.dockerignore` | Ignora arquivos desnecessarios no contexto de build. |
| `k8s/deployment.yaml` | Deployment com probes, recursos e `envFrom`. |
| `k8s/service.yaml` | Service para exposicao da aplicacao no cluster. |
| `k8s/kustomization.yaml` | Entrada Kustomize para aplicar os manifests. |
| `k8s/configmap.yaml` | Gerado quando existem variaveis nao sensiveis. |
| `k8s/secret.yaml` | Gerado quando existem variaveis sensiveis por heuristica. |
| `k8s/hpa.yaml` | Gerado quando `enable_hpa=true`. |
| `k8s/networkpolicy.yaml` | Gerado quando `enable_network_policy=true`. |
| `scripts/build-push.sh` | Build e push da imagem Docker. |
| `scripts/deploy.sh` | Apply e rollout status no Kubernetes. |
| `scripts/rollback.sh` | Rollback e validacao de rollout no Kubernetes. |

## Variaveis de Ambiente

| Variavel | Padrao | Obrigatoria | Uso |
| --- | --- | --- | --- |
| `REDIS_URL` | vazio | Nao | Ativa persistencia de sessoes em Redis com TTL de 30 min. Se ausente, usa memoria local. |
| `ALLOWED_ORIGINS` | `*` | Nao | CORS (origens separadas por virgula). |
| `ENV` | `development` | Nao | Ambiente de execucao (operacional, usado em compose). |
| `KUBEFORGE_SESSION_BACKEND` | `redis` no compose | Nao | Variavel operacional/legada; a API decide por `REDIS_URL`. |
| `SESSION_TTL_MINUTES` | `30` no compose | Nao | Variavel operacional/legada; o codigo atual usa TTL fixo de 30 min. |
| `KUBEFORGE_ALLOWED_SOURCE_DIR` | `/app` no compose | Nao | Variavel operacional/legada para politicas de pasta local. |

## Deploy no Kubernetes

Manifestos prontos para deploy da propria API estao em `k8s/`:

```bash
kubectl apply -k k8s/
```

## Como adicionar suporte a nova linguagem

1. Adicione deteccao em `analyzer/detector.py`.
2. Adicione parse de porta/comando/env em `analyzer/parser.py`.
3. Atualize defaults e overrides em `analyzer/rules.py`.
4. Crie template Docker em `templates/<linguagem>/`.
5. Ajuste `generator/dockerfile_gen.py` para mapear o novo template.
6. Atualize `generator/dockerignore_gen.py` com padroes especificos.
7. Crie/atualize testes em `tests/test_detector.py`, `tests/test_parser.py` e `tests/test_generators.py`.
8. Rode `uv run pytest -q`, `uv run ruff check .` e `uv run mypy --ignore-missing-imports .`.

## Testes e qualidade

```bash
uv run pytest --cov=. --cov-report=term-missing
uv run ruff check .
uv run mypy --ignore-missing-imports .
```

## Documentacao complementar

- Conceitos Docker: `docs/docker-fundamentos.md`
- Conceitos Kubernetes: `docs/kubernetes-fundamentos.md`
- Arquitetura do sistema: `docs/ARCHITECTURE.md`

## Licenca

Este projeto e distribuido sob a licenca MIT.
