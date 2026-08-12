# AGENTS.md - KubeForge

## Projeto
KubeForge e uma API FastAPI que analisa projetos de software e gera
Dockerfiles e manifestos Kubernetes automaticamente.

## Stack
- Python 3.12 com uv como gerenciador de pacotes
- FastAPI + Uvicorn
- Jinja2 para templates
- pytest para testes

## Estrutura
- analyzer/      deteccao de linguagem e parsing de projetos
- generator/     geracao de Dockerfile, .dockerignore e manifestos K8s
- templates/     templates Jinja2 por linguagem (node, python, java, go, dotnet) + k8s
- views/         frontend estatico (index.html)
- tests/         testes pytest
- k8s/           manifestos para deploy do proprio KubeForge

## Regras de codigo
- Type hints obrigatorios em todas as funcoes
- Docstrings em todas as funcoes publicas
- Comentarios em portugues
- Testes para qualquer nova funcao publica
- Nunca commitar .env ou valores reais de secrets

## Comandos uteis
- Instalar deps: uv sync
- Rodar servidor: uv run uvicorn main:app --reload
- Rodar testes: uv run pytest
- Build Docker: docker build -t kubeforge .
- Subir ambiente: docker-compose up
