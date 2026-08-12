# ============================================================
# Estágio 1 — builder: resolve e instala dependências com uv
# ============================================================
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Instala uv apenas no estágio de build — não vai para a imagem final.
RUN pip install --no-cache-dir uv

# Copia manifestos de dependência primeiro para maximizar cache de camadas.
# Enquanto pyproject.toml e uv.lock não mudarem, esta camada é reutilizada.
COPY pyproject.toml uv.lock ./

# --frozen garante reprodutibilidade pelo uv.lock.
# --no-dev exclui dependências de desenvolvimento.
# --no-install-project: o código só é copiado no estágio de runtime; sem esta
# flag o build quebra caso o projeto passe a declarar um [build-system].
RUN uv sync --frozen --no-dev --no-install-project

# ============================================================
# Estágio 2 — runtime: imagem final enxuta sem toolchain de build
# ============================================================
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Versões fixas: "stable.txt" e "releases/latest" fariam o mesmo commit
# gerar imagens diferentes a cada build, anulando o --frozen do uv acima.
ARG KUBECTL_VERSION=v1.36.3
# O "+" da tag k3s vai codificado como %2B por estar dentro da URL.
ARG K3S_VERSION=v1.36.3%2Bk3s1
ARG DOCKER_CLI_VERSION=29.6.1

# git: necessário para source_type=git em runtime.
# curl: necessário para o HEALTHCHECK do container.
# kubectl: necessário para aplicar manifests.
# k3s: necessário para importar imagem local no runtime (k3s ctr images import).
# docker (apenas o cliente): necessário para o build local em /deploy-full.
#   O daemon NÃO roda aqui; o cliente fala com o socket do host montado no compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && curl -fLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && curl -fLo /usr/local/bin/k3s "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}/k3s" \
    && chmod +x /usr/local/bin/k3s \
    && curl -fLo docker.tgz "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    && tar -xzf docker.tgz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm docker.tgz \
    && rm -rf /var/lib/apt/lists/*

# Cria usuário sem privilégios antes de copiar arquivos.
# O home é necessário: o binário k3s extrai seus dados em $HOME/.rancher antes
# de executar "k3s ctr images import" (etapa 4 de /deploy-full).
RUN groupadd --gid 1001 kubeforge \
    && useradd --uid 1001 --gid 1001 --create-home --shell /usr/sbin/nologin kubeforge

# Copia apenas o virtualenv resolvido do estágio builder.
# O uv, pip e cache de instalação NÃO vêm para esta imagem.
COPY --from=builder /app/.venv /app/.venv

# Copia código-fonte com permissões para o usuário kubeforge.
# Arquivos temporários de análise e deploy usam tempfile (/tmp), não /app.
COPY --chown=kubeforge:kubeforge . .

USER kubeforge

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Usa uvicorn diretamente do .venv via PATH — sem precisar de "uv run".
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
