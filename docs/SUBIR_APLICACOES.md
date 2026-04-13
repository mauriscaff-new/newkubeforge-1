# Guia Pratico: Subir Aplicacoes

Este guia foi feito para ajudar a subir:

1. O proprio KubeForge (API + UI)
2. Uma aplicacao gerada pelo KubeForge (Docker e Kubernetes)

## 1) Requisitos

- Docker Desktop (ou Docker Engine) ativo
- `kubectl` configurado (para fluxo Kubernetes)
- Python 3.12+ e `uv` (para rodar sem Docker)

## 2) Subir o KubeForge (sem Docker)

```bash
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Validar:

```bash
curl http://127.0.0.1:8000/health
```

## 3) Subir o KubeForge (com Docker Compose)

```bash
docker compose up --build -d
docker compose logs -f app
```

Validar:

```bash
curl http://127.0.0.1:8000/health
```

Parar:

```bash
docker compose down
```

## 4) Atalhos via Makefile

```bash
make help
make dev
make dev-logs
make health
make dev-down
```

Outros atalhos úteis:

```bash
make run-local
make test
make lint
make k8s-apply
make k8s-status
make k8s-delete
```

## 5) Fluxo completo para uma aplicacao gerada

### Passo A: Analisar projeto

```bash
curl -X POST http://127.0.0.1:8000/analyze \
  -F "source_type=zip" \
  -F "file=@./meu-projeto.zip"
```

Guarde o `session_id`.

### Passo B: Gerar artefatos

```bash
curl -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "session_id":"SEU_SESSION_ID",
    "app_name":"meu-app",
    "image":"ghcr.io/seu-org/meu-app:latest",
    "namespace":"default",
    "replicas":2,
    "port":8000,
    "enable_hpa":true,
    "enable_network_policy":true
  }'
```

### Passo C: Baixar zip com Dockerfile + K8s

```bash
curl -X POST http://127.0.0.1:8000/download \
  -H "Content-Type: application/json" \
  -d '{"session_id":"SEU_SESSION_ID"}' \
  --output artefatos.zip
```

## 6) Subir aplicacao gerada com Docker

Dentro da pasta descompactada:

```bash
docker build -t meu-app:latest .
docker run --rm -p 8000:8000 meu-app:latest
```

## 7) Subir aplicacao gerada no Kubernetes

Dentro da pasta descompactada:

```bash
kubectl apply -k k8s/
kubectl get pods,svc -n default
kubectl rollout status deployment/meu-app -n default
```

## 8) Troubleshooting rapido

### Docker daemon desligado

Erro comum: falha ao conectar em `dockerDesktopLinuxEngine`.  
Solução: iniciar o Docker Desktop e tentar novamente.

### Porta 8000 ocupada

Troque a porta local:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8090 --reload
```

### `source_type=folder` retornando 403

Isso e esperado quando `KUBEFORGE_ALLOWED_SOURCE_DIR` nao esta configurado ou caminho esta fora da pasta permitida.

Exemplo:

```bash
export KUBEFORGE_ALLOWED_SOURCE_DIR=./sources
```

### Falha de permissao em ZIP

ZIP com caminho malicioso ou muito grande e bloqueado por seguranca (path traversal e limite de tamanho descomprimido).
