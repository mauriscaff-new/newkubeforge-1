"""API principal do KubeForge."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
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


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="KubeForge API", version="0.1.0")
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


def _extract_zip_safely(zip_path: Path, destination: Path) -> None:
    """Extrai zip protegendo contra path traversal."""

    destination_resolved = destination.resolve()

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            if not archive.namelist():
                raise HTTPException(status_code=400, detail="Arquivo ZIP vazio.")
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
            data = await upload.read()
            if not data:
                raise HTTPException(status_code=400, detail="Arquivo ZIP enviado está vazio.")
            zip_path.write_bytes(data)
        else:
            zip_path = Path(payload.source_value or "").expanduser().resolve()
            if not zip_path.exists() or not zip_path.is_file():
                raise HTTPException(status_code=400, detail="Arquivo ZIP informado não existe.")

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


@app.on_event("startup")
def startup_event() -> None:
    """Valida estrutura mínima de templates na inicialização."""

    templates_root = Path(__file__).resolve().parent / "templates"
    missing = [str(templates_root / folder) for folder in TEMPLATE_FOLDERS if not (templates_root / folder).exists()]
    if missing:
        raise RuntimeError(f"Diretórios de templates ausentes: {', '.join(missing)}")
    _cleanup_expired_sessions()


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


@app.exception_handler(HTTPException)
def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Padroniza erros HTTP em JSON."""

    detail = exc.detail if isinstance(exc.detail, str) else "Erro na requisição."
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})
