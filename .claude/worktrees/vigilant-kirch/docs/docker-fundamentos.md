# Parte 2 - Docker: Conceitos Fundamentais

## O que e Docker e por que ele existe

Docker resolve o problema "funciona na minha maquina" ao empacotar codigo e dependencias em containers.
Com isso, o ambiente de execucao fica consistente entre notebook, CI e producao.

Containers sao diferentes de VMs:

- VM virtualiza hardware e leva um SO completo por instancia.
- Container compartilha o kernel do host e isola processos.
- Resultado: menor consumo de memoria, startup mais rapido e melhor densidade.

## Imagens Docker, camadas e cache

Cada instrucao do Dockerfile cria uma camada (layer).

Boas praticas no KubeForge:

- Copiar arquivos de dependencia antes do codigo.
- Instalar dependencias antes de `COPY . .`.
- Usar base enxuta (`python:3.12-slim`).

Isso melhora cache e acelera builds incrementais.

## Dockerfile: fundamentos aplicados

Pontos principais usados no projeto:

- `FROM python:3.12-slim`: imagem base menor.
- `WORKDIR /app`: diretorio padrao de execucao.
- `COPY pyproject.toml uv.lock ./` e `uv sync`: dependencias em camada estavel.
- Usuario sem privilegio (`appuser`) para reduzir risco em producao.
- `CMD ["uv", "run", "uvicorn", ...]` em formato JSON para sinais corretos.

## Multi-stage build

Quando ha etapa de build pesada (ex.: Node.js/Next.js), o ideal e:

1. Stage `builder` com toolchain completo.
2. Stage `runner` so com artefatos finais.

Beneficios:

- imagem final menor;
- menor superficie de ataque;
- menos tempo de pull em deploy.

## .dockerignore

No contexto de build do KubeForge, devem ficar de fora:

- ambientes virtuais;
- caches e artefatos de teste;
- `.env` e arquivos de segredo (`*.pem`, `*.key`, `*.p12`);
- pasta `.git` e arquivos de IDE.

Isso melhora performance e evita vazamento de credenciais.

## Docker Compose e rede

No `docker-compose.yml` atual:

- servico `api` conversa com `redis` pelo hostname `redis`;
- Redis usa volume nomeado (`redis_data`) para persistencia;
- `depends_on` com healthcheck evita corrida na inicializacao.

Lembrete de rede:

- `localhost` dentro do container aponta para o proprio container;
- para chamar outro servico use o nome do servico (`redis:6379`).

## Porta e bind

O Uvicorn deve escutar em `0.0.0.0` dentro do container para aceitar conexoes externas.
Com `ports: "8000:8000"`, o host acessa a API em `http://localhost:8000`.
