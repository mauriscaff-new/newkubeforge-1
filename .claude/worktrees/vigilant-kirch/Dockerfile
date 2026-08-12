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
RUN uv sync --frozen --no-dev

# ============================================================
# Estágio 2 — runtime: imagem final enxuta sem toolchain de build
# ============================================================
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# git: necessário para source_type=git em runtime.
# curl: necessário para o HEALTHCHECK do container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# Cria usuário sem privilégios antes de copiar arquivos.
RUN groupadd --gid 1001 kubeforge \
    && useradd --uid 1001 --gid 1001 --no-create-home --shell /usr/sbin/nologin kubeforge

# Copia apenas o virtualenv resolvido do estágio builder.
# O uv, pip e cache de instalação NÃO vêm para esta imagem.
COPY --from=builder /app/.venv /app/.venv

# Copia código-fonte com permissões para o usuário kubeforge.
COPY --chown=kubeforge:kubeforge . .

# Cria diretório de uploads com permissão correta.
RUN mkdir -p /app/sources && chown -R kubeforge:kubeforge /app/sources

USER kubeforge

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Usa uvicorn diretamente do .venv via PATH — sem precisar de "uv run".
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
