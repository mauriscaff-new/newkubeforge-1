"""API principal do KubeForge."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from textwrap import dedent
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from analyzer.detector import detect
from analyzer.parser import parse
from analyzer.rules import FRAMEWORK_OVERRIDES, LANGUAGE_DEFAULTS
from generator import dockerfile_gen, dockerignore_gen, k8s_gen


SESSION_TTL_SECONDS = 30 * 60
SESSION_PREFIX = "kubeforge:session:"
SESSION_INDEX_KEY = "kubeforge:sessions:index"
SESSION_TEMPDIR_KEY = "kubeforge:sessions:tempdirs"
TEMPLATE_FOLDERS = ("python", "node", "java", "go", "dotnet", "k8s")
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_ZIP_COMPRESSED_BYTES = 25 * 1024 * 1024  # 25 MB
ZIP_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MB
deploy_logger = logging.getLogger("kubeforge.deploy")


class AnalyzeRequest(BaseModel):
    """Payload de análise do projeto."""

    source_type: Literal["folder", "zip", "git"]
    source_value: str | None = None
    git_url: str | None = None


class GenerateRequest(BaseModel):
    """Payload para geração de artefatos."""

    session_id: str = Field(min_length=1)
    app_name: str = Field(default="app", min_length=1)
    image: str = Field(default="kubeforge/app:latest", min_length=1)
    namespace: str = Field(default="default", min_length=1)
    service_type: str = Field(default="ClusterIP", min_length=1)
    replicas: int = Field(default=2, ge=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    max_replicas: int | None = Field(default=None, ge=1)
    health_check_path: str | None = Field(default=None)
    env: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, dict[str, str]] = Field(default_factory=dict)
    enable_hpa: bool = True
    enable_network_policy: bool = True


class DownloadRequest(BaseModel):
    """Payload para download dos artefatos gerados."""

    session_id: str = Field(min_length=1)


class PushToServerRequest(BaseModel):
    """Payload para upload dos arquivos ao servidor remoto."""

    session_id: str = Field(min_length=1)
    ssh_host: str = Field(min_length=1)
    ssh_user: str = Field(min_length=1)
    ssh_key: str | None = None
    ssh_password: str | None = None
    git_url: str | None = None
    git_branch: str | None = None


class BuildImageRequest(BaseModel):
    """Payload para build remoto de imagem Docker."""

    session_id: str = Field(min_length=1)
    ssh_host: str = Field(min_length=1)
    ssh_user: str = Field(min_length=1)
    ssh_key: str | None = None
    ssh_password: str | None = None
    image_name: str = Field(min_length=1)


class DeployRequest(BaseModel):
    """Payload para deploy remoto de manifestos no cluster via SSH."""

    session_id: str = Field(min_length=1)
    ssh_host: str = Field(min_length=1)
    ssh_user: str = Field(min_length=1)
    ssh_key: str | None = None
    ssh_password: str | None = None
    kubeconfig: str = Field(min_length=1)


class DeployFullRequest(BaseModel):
    """Payload para deploy completo automatizado (clone + build + apply)."""

    session_id: str = Field(min_length=1)
    git_url: str = Field(min_length=1)
    app_name: str = Field(min_length=1)
    tag: str = "latest"
    kubeconfig: str = Field(min_length=1)


class RemoteDeployRequest(BaseModel):
    """Payload para deploy remoto via SSH em servidor dedicado."""

    session_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    password: str | None = None
    private_key: str | None = None
    remote_path: str = Field(default="~/kubeforge-app", min_length=1)
    app_name: str | None = Field(default=None, min_length=1)
    host_port: int | None = Field(default=None, ge=1, le=65535)
    container_port: int | None = Field(default=None, ge=1, le=65535)
    env: dict[str, str] = Field(default_factory=dict)
    use_sudo: bool = False


class DeployFromGitRequest(BaseModel):
    """Payload para deploy Kubernetes remoto consolidado em uma chamada."""

    session_id: str = Field(min_length=1)
    git_url: str = Field(min_length=1)
    git_branch: str | None = None
    ssh_host: str = Field(min_length=1)
    ssh_user: str = Field(min_length=1)
    ssh_key: str | None = None
    ssh_password: str | None = None
    image_name: str = Field(min_length=1)
    kubeconfig: str = Field(min_length=1)
    registry_url: str | None = None
    registry_user: str | None = None
    registry_password: str | None = None


class SessionData(BaseModel):
    """Estado serializável da sessão."""

    session_id: str
    project_dir: str
    temp_dir: str
    analysis: dict[str, Any]
    parsed: dict[str, Any]
    generated_files: dict[str, str] = Field(default_factory=dict)
    expires_at: float


def _build_redis_client() -> Any | None:
    """Inicializa cliente Redis quando REDIS_URL está configurada."""

    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None

    try:
        from redis import Redis

        client = Redis.from_url(redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        # Fallback automático para memória se Redis não estiver acessível.
        return None


_redis_client = _build_redis_client()
_memory_sessions: dict[str, SessionData] = {}


def _allowed_origins() -> list[str]:
    """Lê ALLOWED_ORIGINS e retorna lista de origens permitidas."""

    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if not raw:
        return ["*"]
    if raw == "*":
        return ["*"]
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["*"]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Valida estrutura minima de templates na inicializacao."""

    templates_root = Path(__file__).resolve().parent / "templates"
    missing = [str(templates_root / folder) for folder in TEMPLATE_FOLDERS if not (templates_root / folder).exists()]
    if missing:
        raise RuntimeError(f"Diretórios de templates ausentes: {', '.join(missing)}")
    _cleanup_expired_sessions()
    yield


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="KubeForge API", version="0.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, cast(Any, _rate_limit_exceeded_handler))
app.mount("/static", StaticFiles(directory="views"), name="static")

origins = _allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _session_key(session_id: str) -> str:
    """Monta chave Redis da sessão."""

    return f"{SESSION_PREFIX}{session_id}"


def _session_expiration() -> float:
    """Retorna timestamp de expiração da sessão."""

    return time.time() + SESSION_TTL_SECONDS


def _cleanup_temp_dir(temp_dir: str) -> None:
    """Remove diretório temporário da sessão."""

    shutil.rmtree(temp_dir, ignore_errors=True)


def _cleanup_expired_sessions() -> None:
    """Remove sessões expiradas e limpa diretórios temporários."""

    now = time.time()

    if _redis_client is not None:
        expired_ids = _redis_client.zrangebyscore(SESSION_INDEX_KEY, 0, now)
        if expired_ids:
            pipe = _redis_client.pipeline()
            for session_id in expired_ids:
                temp_dir = _redis_client.hget(SESSION_TEMPDIR_KEY, session_id)
                if isinstance(temp_dir, str) and temp_dir:
                    _cleanup_temp_dir(temp_dir)
                pipe.delete(_session_key(session_id))
                pipe.zrem(SESSION_INDEX_KEY, session_id)
                pipe.hdel(SESSION_TEMPDIR_KEY, session_id)
            pipe.execute()

    expired_memory = [sid for sid, session in _memory_sessions.items() if session.expires_at <= now]
    for session_id in expired_memory:
        session = _memory_sessions.pop(session_id)
        _cleanup_temp_dir(session.temp_dir)


def _save_session(session: SessionData) -> SessionData:
    """Persiste sessão em Redis ou memória, renovando TTL."""

    updated = session.model_copy(update={"expires_at": _session_expiration()})

    if _redis_client is not None:
        payload = updated.model_dump_json()
        pipe = _redis_client.pipeline()
        pipe.setex(_session_key(updated.session_id), SESSION_TTL_SECONDS, payload)
        pipe.zadd(SESSION_INDEX_KEY, {updated.session_id: updated.expires_at})
        pipe.hset(SESSION_TEMPDIR_KEY, updated.session_id, updated.temp_dir)
        pipe.execute()
        return updated

    _memory_sessions[updated.session_id] = updated
    return updated


def _get_session(session_id: str) -> SessionData | None:
    """Obtém sessão existente e válida."""

    _cleanup_expired_sessions()

    if _redis_client is not None:
        raw = _redis_client.get(_session_key(session_id))
        if raw is None:
            return None
        try:
            return SessionData.model_validate_json(raw)
        except ValidationError:
            return None

    return _memory_sessions.get(session_id)


def _delete_session(session_id: str) -> None:
    """Remove sessão e seus artefatos temporários."""

    if _redis_client is not None:
        temp_dir = _redis_client.hget(SESSION_TEMPDIR_KEY, session_id)
        if isinstance(temp_dir, str) and temp_dir:
            _cleanup_temp_dir(temp_dir)
        pipe = _redis_client.pipeline()
        pipe.delete(_session_key(session_id))
        pipe.zrem(SESSION_INDEX_KEY, session_id)
        pipe.hdel(SESSION_TEMPDIR_KEY, session_id)
        pipe.execute()
        return

    session = _memory_sessions.pop(session_id, None)
    if session is not None:
        _cleanup_temp_dir(session.temp_dir)


def _require_session(session_id: str) -> SessionData:
    """Retorna sessão válida ou erro HTTP."""

    session = _get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Sessão inválida ou expirada.")
    return session


def _validate_git_url(git_url: str) -> None:
    """Valida formato de URL Git aceito pela API."""

    if not git_url:
        raise HTTPException(status_code=400, detail="git_url é obrigatório para source_type=git.")
    if git_url.startswith("https://") or git_url.startswith("git@"):
        return
    raise HTTPException(status_code=400, detail="git_url inválido. Use apenas https:// ou git@.")


def _validate_git_branch(branch: str) -> str:
    """Valida nome de branch para uso em git clone remoto."""

    normalized = branch.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="git_branch inválido.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", normalized):
        raise HTTPException(status_code=400, detail="git_branch inválido.")
    return normalized


def _get_allowed_source_dir() -> Path | None:
    """Retorna diretório base permitido para source_type=folder, ou None se desabilitado."""

    raw = os.getenv("KUBEFORGE_ALLOWED_SOURCE_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _validate_folder_source(source_path: Path) -> None:
    """Bloqueia source_type=folder quando fora do diretório permitido."""

    allowed_dir = _get_allowed_source_dir()
    if allowed_dir is None:
        raise HTTPException(
            status_code=403,
            detail="source_type=folder está desabilitado. Configure KUBEFORGE_ALLOWED_SOURCE_DIR para habilitar.",
        )
    try:
        source_path.relative_to(allowed_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Caminho não permitido. Apenas subdiretórios de '{allowed_dir}' são aceitos.",
        ) from exc


def _validate_zip_source_value(zip_path: Path) -> None:
    """Bloqueia source_type=zip com source_value fora do diretório permitido."""

    allowed_dir = _get_allowed_source_dir()
    if allowed_dir is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "source_type=zip com source_value está desabilitado. "
                "Use upload de arquivo ou configure KUBEFORGE_ALLOWED_SOURCE_DIR."
            ),
        )
    try:
        zip_path.relative_to(allowed_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Caminho não permitido. Apenas subdiretórios de '{allowed_dir}' são aceitos.",
        ) from exc


def _validate_zip_compressed_size(zip_path: Path) -> None:
    """Valida tamanho comprimido do arquivo ZIP antes de processar."""

    try:
        compressed_size = zip_path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Não foi possível ler o arquivo ZIP: {exc}") from exc

    if compressed_size > MAX_ZIP_COMPRESSED_BYTES:
        limit_mb = MAX_ZIP_COMPRESSED_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"ZIP excede o limite de {limit_mb}MB comprimido.",
        )


async def _save_upload_zip_with_limit(upload: UploadFile, zip_path: Path) -> None:
    """Salva upload ZIP em disco por streaming, com limite de tamanho comprimido."""

    total_bytes = 0
    limit_mb = MAX_ZIP_COMPRESSED_BYTES // (1024 * 1024)

    with zip_path.open("wb") as handle:
        while True:
            chunk = await upload.read(ZIP_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_ZIP_COMPRESSED_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"ZIP excede o limite de {limit_mb}MB comprimido.",
                )
            handle.write(chunk)

    if total_bytes == 0:
        raise HTTPException(status_code=400, detail="Arquivo ZIP enviado está vazio.")


def _extract_zip_safely(zip_path: Path, destination: Path) -> None:
    """Extrai zip protegendo contra path traversal e ZIP bombs."""

    destination_resolved = destination.resolve()

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            if not archive.namelist():
                raise HTTPException(status_code=400, detail="Arquivo ZIP vazio.")

            # Proteção contra ZIP bomb: soma tamanho descomprimido de todos os membros.
            total_uncompressed = sum(m.file_size for m in archive.infolist())
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                limit_mb = MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)
                raise HTTPException(
                    status_code=400,
                    detail=f"ZIP excede o limite de {limit_mb}MB descomprimido.",
                )

            for member in archive.infolist():
                target_path = (destination / member.filename).resolve()
                try:
                    target_path.relative_to(destination_resolved)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="ZIP inválido: caminho malicioso detectado.") from exc
                archive.extract(member, destination)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Arquivo ZIP inválido ou corrompido.") from exc


def _normalize_project_root(project_root: Path) -> Path:
    """Normaliza raiz quando zip contém um único diretório encapsulador."""

    current = project_root
    while True:
        entries = [entry for entry in current.iterdir()]
        files = [entry for entry in entries if entry.is_file()]
        dirs = [entry for entry in entries if entry.is_dir()]
        if len(dirs) == 1 and not files:
            current = dirs[0]
            continue
        return current


async def _prepare_source(payload: AnalyzeRequest, upload: UploadFile | None) -> tuple[Path, Path]:
    """Prepara código-fonte local para análise (folder/zip/git)."""

    temp_dir = Path(tempfile.mkdtemp(prefix="kubeforge-src-"))
    project_dir = temp_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    if payload.source_type == "folder":
        if not payload.source_value:
            raise HTTPException(status_code=400, detail="source_value é obrigatório para source_type=folder.")
        source_path = Path(payload.source_value).expanduser().resolve()
        _validate_folder_source(source_path)
        if not source_path.exists() or not source_path.is_dir():
            raise HTTPException(status_code=400, detail="Pasta informada não existe ou não é diretório.")
        try:
            shutil.copytree(source_path, project_dir, dirs_exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Falha ao copiar pasta de origem: {exc}") from exc
        return _normalize_project_root(project_dir), temp_dir

    if payload.source_type == "zip":
        if upload is None and not payload.source_value:
            raise HTTPException(status_code=400, detail="Envie arquivo ZIP ou source_value para source_type=zip.")

        if upload is not None:
            zip_path = temp_dir / "upload.zip"
            await _save_upload_zip_with_limit(upload, zip_path)
        else:
            zip_path = Path(payload.source_value or "").expanduser().resolve()
            _validate_zip_source_value(zip_path)
            if not zip_path.exists() or not zip_path.is_file():
                raise HTTPException(status_code=400, detail="Arquivo ZIP informado não existe.")
            _validate_zip_compressed_size(zip_path)

        _extract_zip_safely(zip_path, project_dir)
        return _normalize_project_root(project_dir), temp_dir

    if payload.source_type == "git":
        git_url = payload.git_url or payload.source_value or ""
        _validate_git_url(git_url)
        try:
            completed = subprocess.run(
                ["git", "clone", "--depth", "1", git_url, str(project_dir)],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail="Git não está disponível no servidor.") from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=400, detail="Tempo limite excedido ao clonar repositório Git.") from exc

        if completed.returncode != 0:
            error_text = completed.stderr.strip() or completed.stdout.strip() or "Erro desconhecido."
            raise HTTPException(status_code=400, detail=f"Falha ao clonar repositório Git: {error_text}")

        return _normalize_project_root(project_dir), temp_dir

    raise HTTPException(status_code=400, detail="source_type inválido. Use folder, zip ou git.")


async def _parse_analyze_input(request: Request) -> tuple[AnalyzeRequest, UploadFile | None]:
    """Interpreta payload de /analyze em JSON ou multipart."""

    content_type = request.headers.get("content-type", "").lower()

    if "application/json" in content_type:
        try:
            payload_raw = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="JSON inválido no corpo da requisição.") from exc
        try:
            return AnalyzeRequest.model_validate(payload_raw), None
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        file_obj = form.get("file")
        upload: UploadFile | None = None
        if file_obj is not None and hasattr(file_obj, "read") and hasattr(file_obj, "filename"):
            upload = file_obj  # type: ignore[assignment]
        payload_raw = {
            "source_type": form.get("source_type"),
            "source_value": form.get("source_value"),
            "git_url": form.get("git_url"),
        }
        try:
            return AnalyzeRequest.model_validate(payload_raw), upload
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    raise HTTPException(status_code=400, detail="Content-Type não suportado em /analyze.")


def _resolve_health_check_path(language: str, framework: str) -> str:
    """Define path de health check com base em defaults e overrides."""

    framework_override = FRAMEWORK_OVERRIDES.get(framework, {})
    if "health_check_path" in framework_override:
        return str(framework_override["health_check_path"])

    language_default = LANGUAGE_DEFAULTS.get(language, {})
    return str(language_default.get("health_check_path", "/health"))


def _generate_scripts(app_name: str, image: str, namespace: str) -> dict[str, str]:
    """Gera scripts utilitários de CI/CD em shell."""

    build_push = dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        IMAGE="${{1:-{image}}}"
        docker build -t "$IMAGE" .
        docker push "$IMAGE"
        """
    )

    deploy = dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        NAMESPACE="${{1:-{namespace}}}"
        kubectl apply -n "$NAMESPACE" -f k8s/
        kubectl rollout status deployment/{app_name} -n "$NAMESPACE"
        """
    )

    rollback = dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        NAMESPACE="${{1:-{namespace}}}"
        kubectl rollout undo deployment/{app_name} -n "$NAMESPACE"
        kubectl rollout status deployment/{app_name} -n "$NAMESPACE"
        """
    )

    return {
        "scripts/build-push.sh": build_push,
        "scripts/deploy.sh": deploy,
        "scripts/rollback.sh": rollback,
    }


def _validate_remote_host(host: str) -> str:
    """Valida host remoto para deploy SSH."""

    cleaned = host.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", cleaned):
        raise HTTPException(status_code=400, detail="Host remoto inválido.")
    return cleaned


def _validate_remote_username(username: str) -> str:
    """Valida usuário SSH remoto."""

    cleaned = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        raise HTTPException(status_code=400, detail="Usuário SSH inválido.")
    return cleaned


def _validate_remote_path(remote_path: str) -> str:
    """Valida path remoto para impedir injeções via shell."""

    cleaned = remote_path.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="remote_path é obrigatório.")
    forbidden_tokens = (";", "&", "|", "`", "$(", "\n", "\r", "\x00")
    if any(token in cleaned for token in forbidden_tokens) or any(char.isspace() for char in cleaned):
        raise HTTPException(status_code=400, detail="remote_path contém caracteres não permitidos.")
    return cleaned


def _resolve_remote_image(session: SessionData) -> str:
    """Tenta reaproveitar a imagem do deployment gerado."""

    deployment = session.generated_files.get("k8s/deployment.yaml", "")
    match = re.search(r"^\s*image:\s*([^\s]+)\s*$", deployment, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return "kubeforge/generated:latest"


def _resolve_remote_container_port(explicit_port: int | None, session: SessionData) -> int:
    """Resolve porta do container com fallback seguro."""

    if explicit_port is not None:
        return explicit_port

    for value in (session.analysis.get("port"), session.parsed.get("port")):
        if isinstance(value, int) and 1 <= value <= 65535:
            return value

    deployment = session.generated_files.get("k8s/deployment.yaml", "")
    match = re.search(r"^\s*containerPort:\s*(\d+)\s*$", deployment, re.MULTILINE)
    if match:
        port = int(match.group(1))
        if 1 <= port <= 65535:
            return port
    return 8000


def _resolve_remote_app_name(explicit_name: str | None, session: SessionData) -> str:
    """Resolve nome de serviço/container para deploy remoto."""

    if explicit_name:
        candidate = explicit_name
    else:
        deployment = session.generated_files.get("k8s/deployment.yaml", "")
        match = re.search(r"^\s*name:\s*([^\s]+)\s*$", deployment, re.MULTILINE)
        candidate = match.group(1).strip() if match else "kubeforge-app"

    cleaned = re.sub(r"[^a-z0-9-]+", "-", candidate.lower()).strip("-")
    return cleaned or "kubeforge-app"


def _render_remote_env_file(env: dict[str, str]) -> str:
    """Renderiza arquivo .env para compose remoto."""

    if not env:
        return ""

    lines: list[str] = []
    for key in sorted(env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise HTTPException(status_code=400, detail=f"Variável de ambiente inválida: {key}")
        value = str(env[key]).replace("\r\n", "\n").replace("\n", "\\n")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _render_remote_compose_file(
    service_name: str,
    image: str,
    container_port: int,
    host_port: int,
    include_env_file: bool,
) -> str:
    """Gera docker-compose de deploy remoto."""

    lines = [
        "services:",
        f"  {service_name}:",
        f"    container_name: {service_name}",
        "    build:",
        "      context: .",
        "      dockerfile: Dockerfile",
        f"    image: {image}",
        "    restart: unless-stopped",
        "    ports:",
        f'      - "{host_port}:{container_port}"',
    ]
    if include_env_file:
        lines.extend(["    env_file:", "      - .env.deploy"])
    return "\n".join(lines) + "\n"


def _truncate_command_output(output: str, max_chars: int = 12_000) -> str:
    """Trunca saída textual para evitar respostas enormes."""

    normalized = output.strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars]}...\n[saída truncada]"


def _redact_sensitive_text(text: str, secrets: list[str] | None = None) -> str:
    """Remove segredos conhecidos de um texto para logs e respostas."""

    if not secrets:
        return text

    redacted = text
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "***")
        redacted = redacted.replace(shlex.quote(secret), "***")
    return redacted


def _build_command_diagnostics(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    message: str,
    *,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Monta diagnóstico detalhado e seguro para falhas de comandos remotos."""

    safe_command = _truncate_command_output(_redact_sensitive_text(command, secrets), max_chars=1_200)
    safe_stdout = _truncate_command_output(_redact_sensitive_text(stdout, secrets), max_chars=4_000)
    safe_stderr = _truncate_command_output(_redact_sensitive_text(stderr, secrets), max_chars=4_000)
    combined = (stdout + ("\n" + stderr if stderr else "")).strip()
    safe_output = _truncate_command_output(_redact_sensitive_text(combined, secrets), max_chars=6_000)

    detail = _truncate_command_output(
        "\n".join(
            [
                message,
                f"exit_code: {exit_code}",
                f"command: {safe_command}",
                "stdout:",
                safe_stdout or "(vazio)",
                "stderr:",
                safe_stderr or "(vazio)",
            ]
        ),
        max_chars=12_000,
    )

    return {
        "message": message,
        "error": detail,
        "output": safe_output,
        "command": safe_command,
        "exit_code": exit_code,
        "stdout": safe_stdout,
        "stderr": safe_stderr,
    }


def _remote_session_path(session_id: str) -> str:
    """Retorna diretório remoto padrão do deploy em 3 etapas."""

    return f"/tmp/kubeforge-deploy/{session_id}"


def _decode_base64_blob(content: str, field_name: str) -> bytes:
    """Decodifica conteúdo base64 com validação."""

    try:
        return base64.b64decode(content, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} inválido em base64.") from exc


def _write_secure_temp_file(temp_dir: Path, name: str, content: bytes) -> Path:
    """Escreve arquivo temporário com permissão restrita."""

    target = temp_dir / name
    target.write_bytes(content)
    target.chmod(0o600)
    return target


def _load_private_key_from_file(paramiko_module: Any, key_path: Path) -> Any:
    """Carrega chave privada SSH a partir de arquivo temporário."""

    for key_class_name in ("RSAKey", "ECDSAKey", "Ed25519Key"):
        key_class = getattr(paramiko_module, key_class_name, None)
        if key_class is None:
            continue
        try:
            return key_class.from_private_key_file(str(key_path))
        except Exception:
            continue
    raise HTTPException(status_code=400, detail="ssh_key inválida ou formato não suportado.")


def _build_bundle_with_session_files(session: SessionData, target_dir: Path) -> Path:
    """Monta diretório local com código-fonte + arquivos gerados."""

    bundle_dir = target_dir / "bundle"
    shutil.copytree(Path(session.project_dir), bundle_dir, dirs_exist_ok=True)

    for relative_name, content in session.generated_files.items():
        file_path = bundle_dir / relative_name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    return bundle_dir


def _build_generated_only_bundle(session: SessionData, target_dir: Path) -> Path:
    """Monta diretório local contendo apenas arquivos gerados pelo KubeForge."""

    bundle_dir = target_dir / "generated"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    for relative_name, content in session.generated_files.items():
        file_path = bundle_dir / relative_name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    return bundle_dir


def _build_k8s_only_bundle(session: SessionData, target_dir: Path) -> Path:
    """Monta diretório local contendo somente a pasta k8s."""

    k8s_dir = target_dir / "k8s"
    k8s_dir.mkdir(parents=True, exist_ok=True)
    wrote_any = False
    for relative_name, content in session.generated_files.items():
        if not relative_name.startswith("k8s/"):
            continue
        wrote_any = True
        filename = relative_name.split("/", 1)[1]
        file_path = k8s_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    if not wrote_any:
        raise HTTPException(status_code=400, detail="Sessão não possui arquivos k8s/ gerados.")
    return k8s_dir


def _sftp_mkdir_p(sftp: Any, remote_dir: str) -> None:
    """Cria diretório remoto recursivamente via SFTP."""

    normalized = PurePosixPath(remote_dir).as_posix()
    if not normalized:
        return

    parts = [part for part in PurePosixPath(normalized).parts if part not in ("/", "")]
    current = "/" if normalized.startswith("/") else ""
    for part in parts:
        current = f"{current.rstrip('/')}/{part}" if current else part
        try:
            sftp.stat(current)
        except Exception:
            sftp.mkdir(current)


def _upload_tree_via_sftp(ssh_client: Any, local_root: Path, remote_root: str) -> None:
    """Envia árvore de arquivos local para diretório remoto via SFTP."""

    sftp = ssh_client.open_sftp()
    try:
        _sftp_mkdir_p(sftp, remote_root)

        for directory in sorted((path for path in local_root.rglob("*") if path.is_dir()), key=lambda p: len(p.parts)):
            relative_dir = directory.relative_to(local_root).as_posix()
            remote_dir = PurePosixPath(remote_root, relative_dir).as_posix()
            _sftp_mkdir_p(sftp, remote_dir)

        for file_path in sorted(path for path in local_root.rglob("*") if path.is_file()):
            relative_file = file_path.relative_to(local_root).as_posix()
            remote_file = PurePosixPath(remote_root, relative_file).as_posix()
            parent_dir = str(PurePosixPath(remote_file).parent)
            _sftp_mkdir_p(sftp, parent_dir)
            sftp.put(str(file_path), remote_file)
    finally:
        sftp.close()


def _open_ssh_client(
    ssh_host: str,
    ssh_user: str,
    temp_dir: Path,
    ssh_key_base64: str | None = None,
    ssh_password: str | None = None,
) -> Any:
    """Abre conexão SSH usando chave privada em base64 ou senha."""

    host = _validate_remote_host(ssh_host)
    user = _validate_remote_username(ssh_user)

    try:
        import paramiko  # type: ignore[import-untyped]
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Dependência ausente para SSH: instale 'paramiko' e reinicie a API.",
        ) from exc

    key_raw = (ssh_key_base64 or "").strip()
    password_raw = ssh_password or ""
    if not key_raw and not password_raw:
        raise HTTPException(status_code=400, detail="Informe ssh_key (base64) ou ssh_password.")

    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs: dict[str, Any] = {
            "hostname": host,
            "port": 22,
            "username": user,
            "timeout": 20,
            "banner_timeout": 20,
            "auth_timeout": 20,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if key_raw:
            key_content = _decode_base64_blob(key_raw, "ssh_key")
            key_path = _write_secure_temp_file(temp_dir, "id_ssh", key_content)
            connect_kwargs["pkey"] = _load_private_key_from_file(paramiko, key_path)
        else:
            connect_kwargs["password"] = password_raw

        ssh_client.connect(**connect_kwargs)
    except HTTPException:
        ssh_client.close()
        raise
    except Exception as exc:
        ssh_client.close()
        raise HTTPException(status_code=500, detail=f"Falha ao conectar via SSH em {host}: {exc}") from exc

    return ssh_client


def _exec_ssh_command(ssh_client: Any, command: str, timeout: int) -> tuple[int, str, str]:
    """Executa comando remoto via SSH e retorna status/stdout/stderr."""

    _stdin, stdout, stderr = ssh_client.exec_command(command, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    out_text = stdout.read().decode("utf-8", errors="replace").strip()
    err_text = stderr.read().decode("utf-8", errors="replace").strip()
    return exit_status, out_text, err_text


def _resolve_remote_home(ssh_client: Any) -> str | None:
    """Obtém HOME do usuário remoto."""

    status, output, _error = _exec_ssh_command(ssh_client, 'printf "%s" "$HOME"', timeout=10)
    if status != 0:
        return None
    home = output.strip()
    return home or None


def _normalize_private_key(private_key: str) -> str:
    """Normaliza chave privada para leitura via Paramiko."""

    normalized = private_key.strip()
    if "\\n" in normalized and "\n" not in normalized:
        normalized = normalized.replace("\\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _load_private_key(paramiko_module: Any, private_key: str) -> Any:
    """Tenta carregar chave privada em formatos RSA/ECDSA/ED25519."""

    key_body = _normalize_private_key(private_key)
    for key_class_name in ("RSAKey", "ECDSAKey", "Ed25519Key"):
        key_class = getattr(paramiko_module, key_class_name, None)
        if key_class is None:
            continue
        try:
            return key_class.from_private_key(io.StringIO(key_body))
        except Exception:
            continue
    raise HTTPException(status_code=400, detail="Chave privada SSH inválida.")


def _deploy_remote_with_ssh(payload: RemoteDeployRequest, session: SessionData) -> str:
    """Realiza deploy remoto com build+up em Docker Compose via SSH."""

    if not payload.password and not payload.private_key:
        raise HTTPException(status_code=400, detail="Informe senha ou chave privada para autenticação SSH.")

    host = _validate_remote_host(payload.host)
    username = _validate_remote_username(payload.username)
    requested_remote_path = _validate_remote_path(payload.remote_path)
    deploy_logger.info(
        "Deploy remoto iniciado host=%s port=%s user=%s remote_path=%s sudo=%s",
        host,
        payload.port,
        username,
        requested_remote_path,
        payload.use_sudo,
    )

    try:
        import paramiko  # type: ignore[import-untyped]
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Dependência ausente para deploy remoto: instale 'paramiko' e reinicie a API.",
        ) from exc

    service_name = _resolve_remote_app_name(payload.app_name, session)
    image = _resolve_remote_image(session)
    container_port = _resolve_remote_container_port(payload.container_port, session)
    host_port = payload.host_port or container_port
    env_file_content = _render_remote_env_file(payload.env)
    compose_content = _render_remote_compose_file(
        service_name=service_name,
        image=image,
        container_port=container_port,
        host_port=host_port,
        include_env_file=bool(env_file_content),
    )

    with tempfile.TemporaryDirectory(prefix="kubeforge-remote-deploy-") as temp_dir:
        temp_path = Path(temp_dir)
        bundle_root = temp_path / "bundle"
        app_dir = bundle_root / "app"

        try:
            shutil.copytree(Path(session.project_dir), app_dir, dirs_exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Falha ao preparar bundle de deploy: {exc}") from exc

        for filename in ("Dockerfile", ".dockerignore"):
            content = session.generated_files.get(filename)
            if content is not None:
                (app_dir / filename).write_text(content, encoding="utf-8")

        (app_dir / "docker-compose.deploy.yml").write_text(compose_content, encoding="utf-8")
        if env_file_content:
            (app_dir / ".env.deploy").write_text(env_file_content, encoding="utf-8")

        archive_path = temp_path / "bundle.tar.gz"
        with tarfile.open(archive_path, mode="w:gz") as archive:
            archive.add(app_dir, arcname="app")

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": host,
            "port": payload.port,
            "username": username,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if payload.private_key:
            connect_kwargs["pkey"] = _load_private_key(paramiko, payload.private_key)
        else:
            connect_kwargs["password"] = payload.password or ""

        try:
            ssh_client.connect(**connect_kwargs)
            deploy_logger.info("SSH conectado host=%s user=%s", host, username)

            remote_home = _resolve_remote_home(ssh_client)
            remote_path = requested_remote_path
            if remote_path == "~":
                if not remote_home:
                    raise HTTPException(
                        status_code=500,
                        detail="Não foi possível resolver $HOME remoto. Use um caminho absoluto em remote_path.",
                    )
                remote_path = remote_home
            elif remote_path.startswith("~/"):
                if not remote_home:
                    raise HTTPException(
                        status_code=500,
                        detail="Não foi possível resolver $HOME remoto. Use um caminho absoluto em remote_path.",
                    )
                remote_path = f"{remote_home}/{remote_path[2:]}"

            remote_path_quoted = shlex.quote(remote_path)
            remote_archive = f"{remote_path.rstrip('/')}/kubeforge-bundle.tar.gz"
            remote_archive_quoted = shlex.quote(remote_archive)

            mkdir_status, mkdir_out, mkdir_err = _exec_ssh_command(
                ssh_client,
                f"mkdir -p {remote_path_quoted}",
                timeout=30,
            )
            if mkdir_status != 0:
                diagnostics = _build_command_diagnostics(
                    command=f"mkdir -p {remote_path_quoted}",
                    exit_code=mkdir_status,
                    stdout=mkdir_out,
                    stderr=mkdir_err,
                    message="Falha ao criar diretório remoto.",
                )
                deploy_logger.error(
                    "Falha mkdir remoto host=%s path=%s command=%s stderr=%s",
                    host,
                    remote_path,
                    diagnostics["command"],
                    diagnostics["stderr"],
                )
                raise HTTPException(status_code=500, detail=diagnostics["error"])

            sftp = ssh_client.open_sftp()
            try:
                sftp.put(str(archive_path), remote_archive)
                deploy_logger.info(
                    "Bundle enviado host=%s remote_archive=%s bytes=%s",
                    host,
                    remote_archive,
                    archive_path.stat().st_size,
                )
            finally:
                sftp.close()

            use_sudo_flag = "1" if payload.use_sudo else "0"
            deploy_command = dedent(
                f"""\
                set -euo pipefail
                mkdir -p {remote_path_quoted}
                tar -xzf {remote_archive_quoted} -C {remote_path_quoted} --strip-components=1
                rm -f {remote_archive_quoted}
                cd {remote_path_quoted}

                if docker compose version >/dev/null 2>&1; then
                  if [ "{use_sudo_flag}" = "1" ]; then
                    sudo -n docker compose -f docker-compose.deploy.yml up -d --build
                    sudo -n docker compose -f docker-compose.deploy.yml ps
                  else
                    docker compose -f docker-compose.deploy.yml up -d --build
                    docker compose -f docker-compose.deploy.yml ps
                  fi
                elif command -v docker-compose >/dev/null 2>&1; then
                  if [ "{use_sudo_flag}" = "1" ]; then
                    sudo -n docker-compose -f docker-compose.deploy.yml up -d --build
                    sudo -n docker-compose -f docker-compose.deploy.yml ps
                  else
                    docker-compose -f docker-compose.deploy.yml up -d --build
                    docker-compose -f docker-compose.deploy.yml ps
                  fi
                else
                  echo "Docker Compose não encontrado no servidor remoto." >&2
                  exit 1
                fi
                """
            )

            deploy_status, deploy_out, deploy_err = _exec_ssh_command(ssh_client, deploy_command, timeout=360)
            if deploy_status != 0:
                diagnostics = _build_command_diagnostics(
                    command=deploy_command,
                    exit_code=deploy_status,
                    stdout=deploy_out,
                    stderr=deploy_err,
                    message="Falha ao executar deploy remoto.",
                )
                deploy_logger.error(
                    "Falha compose remoto host=%s status=%s command=%s stderr=%s",
                    host,
                    deploy_status,
                    diagnostics["command"],
                    diagnostics["stderr"],
                )
                raise HTTPException(status_code=500, detail=diagnostics["error"])

            output_parts = [
                f"Servidor: {host}",
                f"Diretório remoto: {remote_path}",
            ]
            if deploy_out:
                output_parts.append(deploy_out)
            if deploy_err:
                output_parts.append(deploy_err)
            deploy_logger.info("Deploy remoto concluído host=%s remote_path=%s", host, remote_path)
            return _truncate_command_output("\n\n".join(output_parts))

        except HTTPException as exc:
            deploy_logger.warning("Deploy remoto interrompido host=%s detail=%s", host, exc.detail)
            raise
        except Exception as exc:
            deploy_logger.exception("Falha inesperada no deploy remoto host=%s", host)
            raise HTTPException(status_code=500, detail=f"Falha no deploy remoto via SSH: {exc}") from exc
        finally:
            ssh_client.close()


@app.get("/", response_model=None)
def root(request: Request) -> Any:
    """Serve frontend para navegador e JSON para clientes de API."""

    accept = request.headers.get("accept", "").lower()
    index_file = Path(__file__).resolve().parent / "views" / "index.html"

    if "text/html" in accept and index_file.exists():
        return FileResponse(index_file)

    return {"name": "KubeForge API"}


@app.get("/estudos", response_model=None)
def studies_page() -> FileResponse:
    """Serve página de estudos com documentação prática da plataforma."""

    studies_file = Path(__file__).resolve().parent / "views" / "estudos.html"
    if not studies_file.exists():
        raise HTTPException(status_code=404, detail="Página de estudos não encontrada.")
    return FileResponse(studies_file)


@app.get("/health")
def health() -> dict[str, str]:
    """Health check da aplicação."""

    return {"status": "ok"}


@app.post("/analyze")
@limiter.limit("10/minute")
async def analyze(request: Request) -> dict[str, Any]:
    """Analisa projeto e cria sessão temporária."""

    _cleanup_expired_sessions()
    payload, file = await _parse_analyze_input(request)
    project_dir, temp_dir = await _prepare_source(payload, file)

    detection = detect(str(project_dir))
    if detection.get("language") == "unknown":
        _cleanup_temp_dir(str(temp_dir))
        raise HTTPException(
            status_code=422,
            detail="Não foi possível detectar a linguagem do projeto enviado.",
        )
    parsed = parse(str(project_dir), detection["language"], detection["framework"])
    analysis = {
        **detection,
        **parsed,
        "health_check_path": _resolve_health_check_path(detection["language"], detection["framework"]),
    }

    session = SessionData(
        session_id=str(uuid4()),
        project_dir=str(project_dir),
        temp_dir=str(temp_dir),
        analysis=analysis,
        parsed=parsed,
        expires_at=_session_expiration(),
    )
    stored = _save_session(session)

    expires_at_iso = datetime.fromtimestamp(stored.expires_at, tz=UTC).isoformat()
    return {"session_id": stored.session_id, "analysis": analysis, "expires_at": expires_at_iso}


@app.post("/generate")
def generate(payload: GenerateRequest) -> dict[str, Any]:
    """Gera Dockerfile, .dockerignore, manifestos K8s e scripts."""

    _cleanup_expired_sessions()
    session = _require_session(payload.session_id)

    language = str(session.analysis.get("language", "unknown"))
    detected_port = int(session.analysis.get("port", 8000))
    final_port = payload.port if payload.port is not None else detected_port
    health_check_path = payload.health_check_path or str(session.analysis.get("health_check_path", "/health"))

    docker_context: dict[str, Any] = {
        "language": language,
        "framework": str(session.analysis.get("framework", "unknown")),
        "has_build_step": bool(session.analysis.get("has_build_step", False)),
        "build_tool": str(session.analysis.get("build_tool", "unknown")),
        "has_lockfile": bool(session.analysis.get("has_lockfile", False)),
        "package_manager": str(session.analysis.get("package_manager", "")),
        "port": final_port,
        "start_command": str(session.analysis.get("start_command", "")),
    }
    dockerfile_content = dockerfile_gen.generate(docker_context)
    dockerignore_content = dockerignore_gen.generate(language)

    env_values = {key: "" for key in session.parsed.get("env_vars", []) if isinstance(key, str)}
    env_values.update(payload.env)

    k8s_context: dict[str, Any] = {
        "app_name": payload.app_name,
        "image": payload.image,
        "namespace": payload.namespace,
        "service_type": payload.service_type,
        "replicas": payload.replicas,
        "port": final_port,
        "max_replicas": payload.max_replicas,
        "health_check_path": health_check_path,
        "env_values": env_values,
        "resources": payload.resources,
        "enable_hpa": payload.enable_hpa,
        "enable_network_policy": payload.enable_network_policy,
    }
    k8s_files = k8s_gen.generate(k8s_context)
    scripts = _generate_scripts(payload.app_name, payload.image, payload.namespace)

    generated_files: dict[str, str] = {
        "Dockerfile": dockerfile_content,
        ".dockerignore": dockerignore_content,
    }
    generated_files.update({f"k8s/{filename}": content for filename, content in k8s_files.items()})
    generated_files.update(scripts)

    updated_session = session.model_copy(update={"generated_files": generated_files})
    _save_session(updated_session)

    return {
        "session_id": payload.session_id,
        "files": sorted(generated_files.keys()),
        "file_contents": generated_files,
    }


@app.post("/download")
def download(payload: DownloadRequest) -> StreamingResponse:
    """Empacota artefatos gerados e retorna ZIP em streaming."""

    _cleanup_expired_sessions()
    session = _require_session(payload.session_id)

    if not session.generated_files:
        raise HTTPException(status_code=400, detail="Nenhum artefato gerado para esta sessão.")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in session.generated_files.items():
            archive.writestr(filename, content)
    zip_buffer.seek(0)

    _delete_session(payload.session_id)

    headers = {"Content-Disposition": f'attachment; filename="kubeforge-{payload.session_id}.zip"'}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


@app.post("/deploy-full")
@limiter.limit("3/minute")
async def deploy_full(request: Request, payload: DeployFullRequest) -> StreamingResponse:
    """Executa fluxo completo de deploy e transmite logs em tempo real."""

    _ = request

    async def stream() -> Any:
        etapa_atual = "1/4"

        try:
            _cleanup_expired_sessions()
            session = _require_session(payload.session_id)
            if not session.generated_files:
                yield "[ERRO na etapa 1/4] Execute /generate antes de /deploy-full.\n"
                return

            git_url = payload.git_url.strip()
            app_name = payload.app_name.strip()
            tag = (payload.tag or "latest").strip() or "latest"
            _validate_git_url(git_url)

            try:
                kubeconfig_bytes = _decode_base64_blob(payload.kubeconfig, "kubeconfig")
            except HTTPException as exc:
                yield f"[ERRO na etapa 4/4] {exc.detail}\n"
                return

            with tempfile.TemporaryDirectory(prefix="kubeforge-full-deploy-") as temp_dir:
                temp_path = Path(temp_dir)
                app_source_dir = temp_path / "app-source"

                etapa_atual = "1/4"
                yield "[ETAPA 1/4] Clonando repositório...\n"
                clone_proc = subprocess.Popen(
                    ["git", "clone", "--depth", "1", git_url, "app-source"],
                    cwd=str(temp_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if clone_proc.stdout is not None:
                    for line in clone_proc.stdout:
                        yield f"[ETAPA 1/4] {line.rstrip()}\n"
                try:
                    clone_code = clone_proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    clone_proc.kill()
                    yield "[ERRO na etapa 1/4] Tempo limite excedido no git clone.\n"
                    return
                if clone_code != 0:
                    yield "[ERRO na etapa 1/4] Falha ao clonar repositório.\n"
                    return

                etapa_atual = "2/4"
                yield "[ETAPA 2/4] Copiando arquivos do KubeForge...\n"
                copied_files: list[str] = []
                for filename, content in session.generated_files.items():
                    target_file: Path | None = None
                    if filename.startswith("k8s/"):
                        target_file = app_source_dir / "k8s" / Path(filename).name
                    elif filename == "Dockerfile":
                        target_file = app_source_dir / "Dockerfile"
                    elif filename == ".dockerignore":
                        target_file = app_source_dir / ".dockerignore"

                    if target_file is None:
                        continue

                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(content, encoding="utf-8")
                    copied_files.append(str(target_file.relative_to(app_source_dir)).replace("\\", "/"))

                k8s_dir = app_source_dir / "k8s"
                if k8s_dir.exists():
                    for yaml_file in list(k8s_dir.glob("*.yaml")) + list(k8s_dir.glob("*.yml")):
                        yaml_content = yaml_file.read_text(encoding="utf-8")
                        yaml_content = yaml_content.replace("imagePullPolicy: IfNotPresent", "imagePullPolicy: Never")
                        yaml_file.write_text(yaml_content, encoding="utf-8")

                copied_display = ", ".join(copied_files) if copied_files else "(nenhum arquivo relevante)"
                yield f"[OK] Arquivos copiados: {copied_display}\n"

                etapa_atual = "3/4"
                yield "[ETAPA 3/4] Buildando imagem Docker...\n"
                image_ref = f"{app_name}:{tag}"
                build_proc = subprocess.Popen(
                    ["docker", "build", "-t", image_ref, "."],
                    cwd=str(app_source_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if build_proc.stdout is not None:
                    for line in build_proc.stdout:
                        yield f"[ETAPA 3/4] {line.rstrip()}\n"
                build_code = build_proc.wait()
                if build_code != 0:
                    yield "[ERRO] Build falhou\n"
                    return

                etapa_atual = "4/4"
                yield "[ETAPA 4/4] Aplicando manifestos no Kubernetes...\n"

                save_image = subprocess.run(
                    ["docker", "save", "-o", "img.tar", image_ref],
                    cwd=str(temp_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if save_image.returncode != 0:
                    err = save_image.stdout.strip() or save_image.stderr.strip() or "docker save falhou."
                    yield f"[ERRO na etapa 4/4] {err}\n"
                    return

                import_image = subprocess.run(
                    ["k3s", "ctr", "images", "import", "img.tar"],
                    cwd=str(temp_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if import_image.returncode != 0:
                    err = import_image.stdout.strip() or import_image.stderr.strip() or "k3s ctr import falhou."
                    yield f"[ERRO na etapa 4/4] {err}\n"
                    return
                yield "[OK] Imagem importada no k3s\n"

                kubeconfig_path = _write_secure_temp_file(temp_path, "kubeconfig", kubeconfig_bytes)
                apply_env = {**os.environ, "KUBECONFIG": str(kubeconfig_path)}
                apply_proc = subprocess.Popen(
                    ["kubectl", "apply", "-k", "app-source/k8s/"],
                    cwd=str(temp_path),
                    env=apply_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if apply_proc.stdout is not None:
                    for line in apply_proc.stdout:
                        yield f"[ETAPA 4/4] {line.rstrip()}\n"
                apply_code = apply_proc.wait()
                if apply_code != 0:
                    yield "[ERRO na etapa 4/4] Falha ao executar kubectl apply.\n"
                    return

                yield "[CONCLUÍDO] Deploy finalizado!\n"
                yield "[CONCLUÍDO] Deploy finalizado com sucesso!\n"

        except Exception as exc:
            yield f"[ERRO na etapa {etapa_atual}] {exc}\n"

    return StreamingResponse(stream(), media_type="text/plain")


@app.post("/push-to-server")
@limiter.limit("5/minute")
def push_to_server(request: Request, payload: PushToServerRequest) -> JSONResponse:
    """Etapa 1: prepara código remoto (clone opcional) e envia artefatos gerados."""

    _ = request
    _cleanup_expired_sessions()
    session = _require_session(payload.session_id)

    if not session.generated_files:
        raise HTTPException(status_code=400, detail="Execute /generate antes de /push-to-server.")

    remote_path = _remote_session_path(payload.session_id)
    deploy_logger.info("push-to-server iniciado session_id=%s host=%s", payload.session_id, payload.ssh_host)

    git_url = (payload.git_url or "").strip()
    git_branch = (payload.git_branch or "").strip()
    if git_url:
        _validate_git_url(git_url)
    if git_branch:
        git_branch = _validate_git_branch(git_branch)

    with tempfile.TemporaryDirectory(prefix="kubeforge-push-") as temp_dir:
        temp_path = Path(temp_dir)
        bundle_dir = _build_generated_only_bundle(session, temp_path)
        ssh_client = _open_ssh_client(
            payload.ssh_host,
            payload.ssh_user,
            temp_path,
            ssh_key_base64=payload.ssh_key,
            ssh_password=payload.ssh_password,
        )
        try:
            remote_path_q = shlex.quote(remote_path)
            remote_parent_q = shlex.quote(PurePosixPath(remote_path).parent.as_posix())

            if git_url:
                branch_opts = f"--branch {shlex.quote(git_branch)} --single-branch " if git_branch else ""
                clone_cmd = (
                    f"rm -rf {remote_path_q} && mkdir -p {remote_parent_q} "
                    f"&& git clone --depth 1 {branch_opts}{shlex.quote(git_url)} {remote_path_q}"
                )
                status, out, err = _exec_ssh_command(ssh_client, clone_cmd, timeout=240)
            else:
                status, out, err = _exec_ssh_command(
                    ssh_client,
                    f"rm -rf {remote_path_q} && mkdir -p {remote_path_q}",
                    timeout=90,
                )
            if status != 0:
                message = err or out or "Falha ao preparar diretório remoto."
                raise HTTPException(status_code=500, detail=_truncate_command_output(message))

            _upload_tree_via_sftp(ssh_client, bundle_dir, remote_path)
        finally:
            ssh_client.close()

    deploy_logger.info("push-to-server concluído session_id=%s remote_path=%s", payload.session_id, remote_path)
    return JSONResponse(content={"status": "uploaded", "remote_path": remote_path})


@app.post("/build-image")
@limiter.limit("5/minute")
def build_image(request: Request, payload: BuildImageRequest) -> JSONResponse:
    """Etapa 2: executa docker build no servidor remoto."""

    _ = request
    _cleanup_expired_sessions()
    _ = _require_session(payload.session_id)

    image_name = payload.image_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", image_name):
        raise HTTPException(status_code=400, detail="image_name inválido.")

    remote_path = _remote_session_path(payload.session_id)
    deploy_logger.info("build-image iniciado session_id=%s host=%s image=%s", payload.session_id, payload.ssh_host, image_name)

    with tempfile.TemporaryDirectory(prefix="kubeforge-build-") as temp_dir:
        temp_path = Path(temp_dir)
        ssh_client = _open_ssh_client(
            payload.ssh_host,
            payload.ssh_user,
            temp_path,
            ssh_key_base64=payload.ssh_key,
            ssh_password=payload.ssh_password,
        )
        try:
            command = f"cd {shlex.quote(remote_path)} && docker build -t {shlex.quote(image_name)} ."
            status, out, err = _exec_ssh_command(ssh_client, command, timeout=1800)
        finally:
            ssh_client.close()

    combined_output = (out + ("\n" + err if err else "")).strip()
    if status != 0:
        raise HTTPException(status_code=500, detail=_truncate_command_output(combined_output or "Falha no docker build remoto."))

    deploy_logger.info("build-image concluído session_id=%s image=%s", payload.session_id, image_name)
    return JSONResponse(content={"status": "built", "image": image_name, "output": _truncate_command_output(combined_output)})


@app.post("/deploy")
@limiter.limit("5/minute")
def deploy(request: Request, payload: DeployRequest) -> JSONResponse:
    """Etapa 3: envia k8s/ e aplica no cluster remoto com kubeconfig fornecido."""

    _ = request
    _cleanup_expired_sessions()
    session = _require_session(payload.session_id)

    if not session.generated_files:
        raise HTTPException(status_code=400, detail="Execute /generate antes de /deploy.")

    remote_path = _remote_session_path(payload.session_id)
    deploy_logger.info("deploy iniciado session_id=%s host=%s", payload.session_id, payload.ssh_host)

    with tempfile.TemporaryDirectory(prefix="kubeforge-deploy-step3-") as temp_dir:
        temp_path = Path(temp_dir)
        k8s_dir = _build_k8s_only_bundle(session, temp_path)
        kubeconfig_bytes = _decode_base64_blob(payload.kubeconfig, "kubeconfig")
        kubeconfig_local = _write_secure_temp_file(temp_path, "kubeconfig", kubeconfig_bytes)

        ssh_client = _open_ssh_client(
            payload.ssh_host,
            payload.ssh_user,
            temp_path,
            ssh_key_base64=payload.ssh_key,
            ssh_password=payload.ssh_password,
        )
        remote_kubeconfig = f"/tmp/kubeforge-kubeconfig-{payload.session_id}"
        remote_kubeconfig_q = shlex.quote(remote_kubeconfig)
        try:
            k8s_remote_path = f"{remote_path.rstrip('/')}/k8s"
            remote_path_q = shlex.quote(remote_path)
            k8s_remote_path_q = shlex.quote(k8s_remote_path)

            status_prepare, out_prepare, err_prepare = _exec_ssh_command(
                ssh_client,
                f"mkdir -p {remote_path_q} && rm -rf {k8s_remote_path_q} && mkdir -p {k8s_remote_path_q}",
                timeout=90,
            )
            if status_prepare != 0:
                message_prepare = err_prepare or out_prepare or "Falha ao preparar diretório k8s remoto."
                raise HTTPException(status_code=500, detail=_truncate_command_output(message_prepare))

            _upload_tree_via_sftp(ssh_client, k8s_dir, k8s_remote_path)

            sftp = ssh_client.open_sftp()
            try:
                sftp.put(str(kubeconfig_local), remote_kubeconfig)
                sftp.chmod(remote_kubeconfig, 0o600)
            finally:
                sftp.close()

            apply_cmd = f"cd {remote_path_q} && KUBECONFIG={remote_kubeconfig_q} kubectl apply -k k8s/"
            status_apply, out_apply, err_apply = _exec_ssh_command(ssh_client, apply_cmd, timeout=300)
        finally:
            try:
                _exec_ssh_command(ssh_client, f"rm -f {remote_kubeconfig_q}", timeout=30)
            except Exception:
                pass
            ssh_client.close()

    output = (out_apply + ("\n" + err_apply if err_apply else "")).strip()
    if status_apply != 0:
        raise HTTPException(status_code=500, detail=_truncate_command_output(output or "Falha ao executar kubectl apply -k k8s/."))

    deploy_logger.info("deploy concluído session_id=%s remote_path=%s", payload.session_id, remote_path)
    return JSONResponse(content={"status": "deployed", "output": _truncate_command_output(output)})


@app.post("/deploy/remote")
@limiter.limit("5/minute")
def deploy_remote(request: Request, payload: RemoteDeployRequest) -> JSONResponse:
    """Executa deploy remoto em servidor dedicado via SSH."""

    _ = request
    _cleanup_expired_sessions()
    session = _require_session(payload.session_id)

    if not session.generated_files:
        raise HTTPException(status_code=400, detail="Execute /generate antes de /deploy/remote.")

    output = _deploy_remote_with_ssh(payload, session)
    return JSONResponse(content={"status": "deployed_remote", "output": output})


@app.post("/deploy/from-git")
@limiter.limit("5/minute")
async def deploy_from_git(request: Request, payload: DeployFromGitRequest) -> StreamingResponse:
    """Executa pipeline completo de deploy Kubernetes remoto a partir de um repositório Git."""

    _ = request
    _cleanup_expired_sessions()
    session = _require_session(payload.session_id)
    if not session.generated_files:
        raise HTTPException(status_code=400, detail="Execute /generate antes de /deploy/from-git.")

    ssh_key = (payload.ssh_key or "").strip()
    ssh_password = payload.ssh_password or ""
    if not ssh_key and not ssh_password:
        raise HTTPException(status_code=400, detail="Informe ssh_key (base64) ou ssh_password.")

    host = _validate_remote_host(payload.ssh_host)
    user = _validate_remote_username(payload.ssh_user)
    git_url = payload.git_url.strip()
    _validate_git_url(git_url)
    git_branch = (payload.git_branch or "").strip()
    if git_branch:
        git_branch = _validate_git_branch(git_branch)

    image_name = payload.image_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", image_name):
        raise HTTPException(status_code=400, detail="image_name inválido.")

    kubeconfig_bytes = _decode_base64_blob(payload.kubeconfig, "kubeconfig")

    registry_url = (payload.registry_url or "").strip()
    registry_user = (payload.registry_user or "").strip()
    registry_password = payload.registry_password or ""
    if registry_url and (not registry_user or not registry_password):
        raise HTTPException(
            status_code=400,
            detail="Ao informar registry_url, também informe registry_user e registry_password.",
        )

    remote_path = _remote_session_path(payload.session_id)
    remote_path_q = shlex.quote(remote_path)
    remote_parent_q = shlex.quote(PurePosixPath(remote_path).parent.as_posix())
    remote_kubeconfig = f"/tmp/kubeforge-kubeconfig-{payload.session_id}"
    remote_kubeconfig_q = shlex.quote(remote_kubeconfig)

    def _event(step: str, status: str, **extra: Any) -> str:
        """Serializa uma linha de evento JSON para o stream."""

        payload_data: dict[str, Any] = {"step": step, "status": status}
        payload_data.update(extra)
        return json.dumps(payload_data, ensure_ascii=False) + "\n"

    async def stream() -> Any:
        total_started_at = time.monotonic()
        completed_steps: list[str] = []
        current_step = "clone"
        ssh_client: Any | None = None

        with tempfile.TemporaryDirectory(prefix="kubeforge-deploy-from-git-") as temp_dir:
            temp_path = Path(temp_dir)

            try:
                generated_bundle = _build_generated_only_bundle(session, temp_path)
                _ = _build_k8s_only_bundle(session, temp_path / "k8s-only")
                kubeconfig_local = _write_secure_temp_file(temp_path, "kubeconfig", kubeconfig_bytes)
                uploaded_files = sum(1 for path in generated_bundle.rglob("*") if path.is_file())

                current_step = "clone"
                clone_started_at = time.monotonic()
                yield _event("clone", "running", message="Clonando repositório...")
                deploy_logger.info("deploy/from-git clone iniciado session_id=%s host=%s", payload.session_id, host)

                ssh_client = _open_ssh_client(
                    host,
                    user,
                    temp_path,
                    ssh_key_base64=ssh_key or None,
                    ssh_password=ssh_password or None,
                )

                branch_opts = f"--branch {shlex.quote(git_branch)} --single-branch " if git_branch else ""
                clone_cmd = (
                    f"rm -rf {remote_path_q} && mkdir -p {remote_parent_q} "
                    f"&& git clone --depth 1 {branch_opts}{shlex.quote(git_url)} {remote_path_q}"
                )
                clone_status, clone_out, clone_err = _exec_ssh_command(ssh_client, clone_cmd, timeout=300)
                if clone_status != 0:
                    diagnostics = _build_command_diagnostics(
                        command=clone_cmd,
                        exit_code=clone_status,
                        stdout=clone_out,
                        stderr=clone_err,
                        message="Falha no git clone remoto.",
                    )
                    deploy_logger.error(
                        "deploy/from-git falha clone session_id=%s host=%s command=%s stderr=%s",
                        payload.session_id,
                        host,
                        diagnostics["command"],
                        diagnostics["stderr"],
                    )
                    yield _event(
                        "clone",
                        "error",
                        **diagnostics,
                    )
                    yield _event("finished", "failed", failed_at="clone", completed_steps=completed_steps)
                    return
                completed_steps.append("clone")
                yield _event(
                    "clone",
                    "done",
                    message="Repositório clonado com sucesso",
                    duration_s=round(time.monotonic() - clone_started_at, 2),
                )

                current_step = "upload"
                upload_started_at = time.monotonic()
                yield _event("upload", "running", message="Enviando artefatos gerados...")
                _upload_tree_via_sftp(ssh_client, generated_bundle, remote_path)
                completed_steps.append("upload")
                yield _event(
                    "upload",
                    "done",
                    message=f"{uploaded_files} arquivos enviados",
                    duration_s=round(time.monotonic() - upload_started_at, 2),
                )

                current_step = "build"
                build_started_at = time.monotonic()
                yield _event("build", "running", message="Construindo imagem Docker...")
                build_cmd = f"cd {remote_path_q} && docker build -t {shlex.quote(image_name)} ."
                build_status, build_out, build_err = _exec_ssh_command(ssh_client, build_cmd, timeout=1800)
                if build_status != 0:
                    diagnostics = _build_command_diagnostics(
                        command=build_cmd,
                        exit_code=build_status,
                        stdout=build_out,
                        stderr=build_err,
                        message="Falha no docker build.",
                    )
                    deploy_logger.error(
                        "deploy/from-git falha build session_id=%s host=%s command=%s stderr=%s",
                        payload.session_id,
                        host,
                        diagnostics["command"],
                        diagnostics["stderr"],
                    )
                    yield _event(
                        "build",
                        "error",
                        **diagnostics,
                    )
                    yield _event("finished", "failed", failed_at="build", completed_steps=completed_steps)
                    return
                completed_steps.append("build")
                yield _event(
                    "build",
                    "done",
                    message=f"Imagem {image_name} construída",
                    duration_s=round(time.monotonic() - build_started_at, 2),
                )

                current_step = "push"
                push_started_at = time.monotonic()
                if registry_url:
                    registry_url_q = shlex.quote(registry_url)
                    registry_user_q = shlex.quote(registry_user)
                    registry_password_q = shlex.quote(registry_password)

                    yield _event("push", "running", message="Enviando imagem para o registry...")
                    login_cmd = (
                        f"cd {remote_path_q} && "
                        f"printf '%s' {registry_password_q} | "
                        f"docker login {registry_url_q} -u {registry_user_q} --password-stdin"
                    )
                    login_status, login_out, login_err = _exec_ssh_command(ssh_client, login_cmd, timeout=600)
                    if login_status != 0:
                        diagnostics = _build_command_diagnostics(
                            command=login_cmd,
                            exit_code=login_status,
                            stdout=login_out,
                            stderr=login_err,
                            message="Falha no docker login.",
                            secrets=[registry_password],
                        )
                        deploy_logger.error(
                            "deploy/from-git falha login registry session_id=%s host=%s command=%s stderr=%s",
                            payload.session_id,
                            host,
                            diagnostics["command"],
                            diagnostics["stderr"],
                        )
                        yield _event(
                            "push",
                            "error",
                            **diagnostics,
                        )
                        yield _event("finished", "failed", failed_at="push", completed_steps=completed_steps)
                        return

                    push_cmd = f"cd {remote_path_q} && docker push {shlex.quote(image_name)}"
                    push_status, push_out, push_err = _exec_ssh_command(ssh_client, push_cmd, timeout=600)
                    if push_status != 0:
                        diagnostics = _build_command_diagnostics(
                            command=push_cmd,
                            exit_code=push_status,
                            stdout=push_out,
                            stderr=push_err,
                            message="Falha no docker push.",
                        )
                        deploy_logger.error(
                            "deploy/from-git falha push session_id=%s host=%s command=%s stderr=%s",
                            payload.session_id,
                            host,
                            diagnostics["command"],
                            diagnostics["stderr"],
                        )
                        yield _event(
                            "push",
                            "error",
                            **diagnostics,
                        )
                        yield _event("finished", "failed", failed_at="push", completed_steps=completed_steps)
                        return

                    completed_steps.append("push")
                    yield _event(
                        "push",
                        "done",
                        message=f"Imagem {image_name} enviada para o registry",
                        duration_s=round(time.monotonic() - push_started_at, 2),
                    )
                else:
                    yield _event("push", "skipped", message="Registry não configurado, assumindo cluster local")

                current_step = "apply"
                apply_started_at = time.monotonic()
                yield _event("apply", "running", message="Aplicando manifestos no cluster...")

                sftp = ssh_client.open_sftp()
                try:
                    sftp.put(str(kubeconfig_local), remote_kubeconfig)
                    sftp.chmod(remote_kubeconfig, 0o600)
                finally:
                    sftp.close()

                apply_cmd = f"cd {remote_path_q} && KUBECONFIG={remote_kubeconfig_q} kubectl apply -k k8s/"
                apply_status, apply_out, apply_err = _exec_ssh_command(ssh_client, apply_cmd, timeout=300)
                apply_output = _truncate_command_output((apply_out + ("\n" + apply_err if apply_err else "")).strip())
                if apply_status != 0:
                    diagnostics = _build_command_diagnostics(
                        command=apply_cmd,
                        exit_code=apply_status,
                        stdout=apply_out,
                        stderr=apply_err,
                        message="Falha no kubectl apply.",
                    )
                    deploy_logger.error(
                        "deploy/from-git falha apply session_id=%s host=%s command=%s stderr=%s",
                        payload.session_id,
                        host,
                        diagnostics["command"],
                        diagnostics["stderr"],
                    )
                    yield _event(
                        "apply",
                        "error",
                        **diagnostics,
                    )
                    yield _event("finished", "failed", failed_at="apply", completed_steps=completed_steps)
                    return

                completed_steps.append("apply")
                yield _event(
                    "apply",
                    "done",
                    message="Deploy realizado com sucesso",
                    output=apply_output,
                    duration_s=round(time.monotonic() - apply_started_at, 2),
                )
                yield _event(
                    "finished",
                    "success",
                    total_duration_s=round(time.monotonic() - total_started_at, 2),
                )
                deploy_logger.info("deploy/from-git concluído session_id=%s host=%s image=%s", payload.session_id, host, image_name)

            except Exception as exc:
                error_text = _truncate_command_output(str(exc) or "Falha inesperada no pipeline.")
                deploy_logger.exception(
                    "deploy/from-git falha inesperada session_id=%s host=%s step=%s",
                    payload.session_id,
                    host,
                    current_step,
                )
                yield _event(
                    current_step,
                    "error",
                    message=f"Falha inesperada na etapa {current_step}",
                    error=error_text,
                    output=error_text,
                    exception_type=type(exc).__name__,
                )
                yield _event("finished", "failed", failed_at=current_step, completed_steps=completed_steps)
            finally:
                if ssh_client is not None:
                    try:
                        _exec_ssh_command(ssh_client, f"rm -f {remote_kubeconfig_q}", timeout=30)
                    except Exception:
                        pass
                    ssh_client.close()

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.exception_handler(HTTPException)
def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Padroniza erros HTTP em JSON."""

    detail = exc.detail if isinstance(exc.detail, str) else "Erro na requisição."
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})
