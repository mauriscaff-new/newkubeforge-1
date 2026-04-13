IMAGE ?= ghcr.io/mauriscaff/kubeforge:latest

.PHONY: dev test lint build push

dev:
	docker-compose up --build

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy --ignore-missing-imports .

build:
	docker build -t $(IMAGE) .

push: build
	docker push $(IMAGE)
