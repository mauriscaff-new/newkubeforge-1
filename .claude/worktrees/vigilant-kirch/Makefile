IMAGE ?= ghcr.io/mauriscaff/kubeforge:latest
APP_PORT ?= 8000

.PHONY: help dev dev-down dev-logs restart health run-local test lint build push k8s-apply k8s-delete k8s-status

help:
	@echo "Comandos disponíveis:"
	@echo "  make dev         - sobe ambiente local com Docker Compose em background"
	@echo "  make dev-down    - derruba ambiente Docker Compose"
	@echo "  make dev-logs    - acompanha logs do serviço app"
	@echo "  make restart     - reinicia ambiente Docker Compose"
	@echo "  make health      - valida /health da API local"
	@echo "  make run-local   - roda API sem Docker com uvicorn em modo reload"
	@echo "  make test        - executa suíte de testes"
	@echo "  make lint        - executa ruff e mypy"
	@echo "  make build       - build da imagem Docker"
	@echo "  make push        - push da imagem Docker"
	@echo "  make k8s-apply   - aplica manifests de k8s/"
	@echo "  make k8s-delete  - remove manifests de k8s/"
	@echo "  make k8s-status  - status dos recursos no namespace kubeforge-system"

dev:
	docker compose up --build -d

dev-down:
	docker compose down

dev-logs:
	docker compose logs -f app

restart: dev-down dev

health:
	curl -fsS http://127.0.0.1:$(APP_PORT)/health

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy --ignore-missing-imports .

build:
	docker build -t $(IMAGE) .

push: build
	docker push $(IMAGE)

run-local:
	uv sync
	uv run uvicorn main:app --host 0.0.0.0 --port $(APP_PORT) --reload

k8s-apply:
	kubectl apply -k k8s/

k8s-delete:
	kubectl delete -k k8s/ --ignore-not-found

k8s-status:
	kubectl get pods,svc,deploy,hpa,ingress -n kubeforge-system
