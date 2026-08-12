# Arquitetura do KubeForge

## Visao geral

O KubeForge segue um pipeline simples e direto:

1. Recebe codigo-fonte (`folder`, `zip` ou `git`) no endpoint `/analyze`.
2. Detecta linguagem/framework e extrai configuracoes tecnicas.
3. Recebe ajustes do usuario no endpoint `/generate`.
4. Renderiza artefatos de Docker, Kubernetes e scripts.
5. Entrega pacote final no endpoint `/download`.

Esse desenho separa claramente "descoberta" (analyze) de "renderizacao" (generate), reduzindo acoplamento entre etapas.

## Pipeline analyzer -> generator

### Fase 1: Analyze

`main.py` coordena:

1. `_prepare_source(...)`:
   - `folder`: copia pasta para diretorio temporario.
   - `zip`: extrai com protecao contra path traversal.
   - `git`: faz `git clone --depth 1` com validacao de URL (`https://` ou `git@`).
2. `analyzer.detector.detect(project_path)`:
   - Detecta linguagem, framework, build tool e necessidade de build step.
   - Walk limitado a 3 niveis e ignora pastas pesadas (`node_modules`, `.git`, `dist`, etc.).
3. `analyzer.parser.parse(project_path, language, framework)`:
   - Resolve porta, comando de start e variaveis de ambiente.
   - Lendo `package.json`, `requirements.txt`, `pyproject.toml`, `.env` e `.env.example`.
4. Monta `analysis` e cria `session_id` com TTL.

Saida principal de `/analyze`:
- `session_id`
- `analysis` (language, framework, build_tool, port, start_command, env_vars, health_check_path)
- `expires_at`

### Fase 2: Generate

`main.py` combina defaults detectados com overrides do usuario e chama:

1. `generator/dockerfile_gen.generate(context)`:
   - Escolhe template por linguagem e `has_build_step`.
2. `generator/dockerignore_gen.generate(language)`:
   - Gera `.dockerignore` por linguagem + padroes comuns.
3. `generator/k8s_gen.generate(context)`:
   - Gera `deployment.yaml`, `service.yaml`, `kustomization.yaml`.
   - Gera condicionalmente `configmap.yaml`, `secret.yaml`, `hpa.yaml`, `networkpolicy.yaml`.
4. `_generate_scripts(...)`:
   - `scripts/build-push.sh`
   - `scripts/deploy.sh`
   - `scripts/rollback.sh`

Saida principal de `/generate`:
- lista de arquivos gerados
- conteudo completo de cada arquivo (para preview no frontend)

### Fase 3: Download

`/download` empacota os arquivos gerados em ZIP via streaming e remove a sessao apos envio.

## Componentes e responsabilidades

- `analyzer/`
  - `detector.py`: identifica stack do projeto.
  - `parser.py`: extrai parametros tecnicos da aplicacao.
  - `rules.py`: defaults por linguagem/framework + defaults K8s.
- `generator/`
  - `dockerfile_gen.py`: renderizacao de Dockerfile.
  - `dockerignore_gen.py`: regras de `.dockerignore`.
  - `k8s_gen.py`: renderizacao dos manifests K8s.
- `templates/`
  - Dockerfiles por linguagem.
  - Objetos Kubernetes em Jinja2.
- `main.py`
  - API FastAPI, sessao, validacoes, rate limiting e orquestracao.
- `views/index.html`
  - SPA sem framework para fluxo analyze/generate/download.

## Decisoes de design

### 1. Jinja2 com StrictUndefined

Todos os geradores usam `StrictUndefined`. Se alguma variavel obrigatoria nao for passada ao template, a renderizacao falha imediatamente.

Motivo:
- evita manifestos parcialmente corretos
- reduz bugs silenciosos em producao
- obriga contrato explicito entre contexto e template

### 2. Session store com TTL de 30 minutos

O estado entre `/analyze` e `/generate` e guardado por `session_id` com TTL de 30 minutos.

Comportamento:
- com `REDIS_URL`: usa Redis (`setex`) e permite escalar para multiplas instancias.
- sem `REDIS_URL`: usa dicionario em memoria para execucao local simples.

Motivo:
- evita reanalise do mesmo projeto em cada request
- simplifica UX da SPA
- garante limpeza automatica de diretorios temporarios

Limitacao atual:
- TTL e fixo em codigo (`30 * 60` segundos), nao configurado por env no runtime.

### 3. Heuristica de Secrets no K8s

`generator/k8s_gen.py` separa vars em `ConfigMap` e `Secret` por nome da chave usando regex sensivel a termos como:

- `password`
- `secret`
- `token`
- `credential`
- `private`
- `auth`
- `api_key`, `apikey`, `key`

Motivo:
- default seguro sem exigir classificacao manual inicial
- reduz chance de vazar segredo em `ConfigMap`

Limitacao:
- heuristica por nome nao substitui governanca de segredo em producao (Vault, External Secrets, Sealed Secrets).

## Como AGENTS.md orienta o desenvolvimento com Codex

`AGENTS.md` e lido automaticamente pelo Codex no inicio de cada sessao e funciona como contrato de engenharia do repositorio.

No KubeForge, ele define:

- contexto de produto (API que gera Docker/K8s)
- stack oficial (Python 3.12, uv, FastAPI, Jinja2, pytest)
- estrutura esperada de pastas
- regras de qualidade (type hints, docstrings, comentarios em portugues, testes)
- comandos padrao de execucao

Efeito pratico:
- reduz ambiguidade nos prompts
- aumenta consistencia entre contribuicoes
- acelera onboarding de novos colaboradores humanos e agentes

## Fluxo resumido de dados

1. Usuario envia fonte.
2. API normaliza fonte em diretorio temporario.
3. Analyzer detecta stack e parametros.
4. Sessao temporaria guarda contexto.
5. Usuario ajusta parametros.
6. Generator renderiza artefatos.
7. Download entrega ZIP final.
