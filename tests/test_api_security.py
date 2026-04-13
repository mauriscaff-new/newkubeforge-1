"""Testes da API principal do KubeForge."""

from __future__ import annotations

import importlib
import io
import sys
import tempfile
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio


@pytest.fixture
def sandbox_source(tmp_path: Path) -> Path:
    source_root = tmp_path / "project"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "pyproject.toml").write_text(
        "[project]\nname='demo'\ndependencies=['fastapi>=0.100.0']\n",
        encoding="utf-8",
    )
    (source_root / ".env.example").write_text("LOG_LEVEL=info\nAPI_KEY=abc\n", encoding="utf-8")
    return source_root


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[httpx.AsyncClient, None]:
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000")
    monkeypatch.setenv("KUBEFORGE_ALLOWED_SOURCE_DIR", tempfile.gettempdir())

    project_root = Path(__file__).resolve().parents[1]
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

    sys.modules.pop("main", None)
    main_module = importlib.import_module("main")
    main_module = importlib.reload(main_module)

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.mark.asyncio
async def test_root_e_health(client: httpx.AsyncClient) -> None:
    root = await client.get("/")
    health = await client.get("/health")

    assert root.status_code == 200
    assert root.json()["name"] == "KubeForge API"
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_rejeita_git_url_invalido(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/analyze",
        json={"source_type": "git", "git_url": "http://example.com/repo.git"},
    )

    assert response.status_code == 400
    assert "git_url inválido" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rejeita_folder_quando_desabilitado(
    client: httpx.AsyncClient,
    sandbox_source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBEFORGE_ALLOWED_SOURCE_DIR", "")

    response = await client.post(
        "/analyze",
        json={"source_type": "folder", "source_value": str(sandbox_source)},
    )

    assert response.status_code == 403
    assert "desabilitado" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rejeita_folder_fora_do_diretorio_permitido(
    client: httpx.AsyncClient,
    sandbox_source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBEFORGE_ALLOWED_SOURCE_DIR", str(sandbox_source))
    unauthorized_dir = sandbox_source.parent

    response = await client.post(
        "/analyze",
        json={"source_type": "folder", "source_value": str(unauthorized_dir)},
    )

    assert response.status_code == 403
    assert "caminho não permitido" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_aceita_folder_no_analyze(client: httpx.AsyncClient, sandbox_source: Path) -> None:
    response = await client.post(
        "/analyze",
        json={"source_type": "folder", "source_value": str(sandbox_source)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["analysis"]["language"] == "python"
    assert body["analysis"]["framework"] == "fastapi"


@pytest.mark.asyncio
async def test_aceita_zip_no_analyze(client: httpx.AsyncClient) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project/requirements.txt", "flask==3.0.0\n")
        archive.writestr("project/app.py", "print('ok')\n")
    buffer.seek(0)

    response = await client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("project.zip", buffer.read(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["analysis"]["language"] == "python"


@pytest.mark.asyncio
async def test_rejeita_zip_bomb(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project/requirements.txt", "flask==3.0.0\n")
    buffer.seek(0)

    original_infolist = zipfile.ZipFile.infolist

    def fake_infolist(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        infos = original_infolist(self)
        if infos:
            infos[0].file_size = 101 * 1024 * 1024
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", fake_infolist)

    response = await client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("zipbomb.zip", buffer.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "excede o limite" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_aplica_rate_limit_no_analyze(client: httpx.AsyncClient, sandbox_source: Path) -> None:
    payload = {"source_type": "folder", "source_value": str(sandbox_source)}

    for _ in range(10):
        response = await client.post("/analyze", json=payload)
        assert response.status_code == 200

    limited_response = await client.post("/analyze", json=payload)
    assert limited_response.status_code == 429


@pytest.mark.asyncio
async def test_validacoes_generate_request(client: httpx.AsyncClient) -> None:
    replicas_invalid = await client.post(
        "/generate",
        json={"session_id": "abc", "replicas": 0},
    )
    port_invalid = await client.post(
        "/generate",
        json={"session_id": "abc", "port": 70000},
    )

    assert replicas_invalid.status_code == 422
    assert port_invalid.status_code == 422


@pytest.mark.asyncio
async def test_fluxo_generate_e_download(client: httpx.AsyncClient, sandbox_source: Path) -> None:
    analyze_response = await client.post(
        "/analyze",
        json={"source_type": "folder", "source_value": str(sandbox_source)},
    )
    assert analyze_response.status_code == 200
    session_id = analyze_response.json()["session_id"]

    generate_response = await client.post(
        "/generate",
        json={
            "session_id": session_id,
            "app_name": "kubeforge",
            "image": "repo/kubeforge:latest",
            "namespace": "default",
            "replicas": 2,
            "port": 8000,
            "env": {"LOG_LEVEL": "info", "API_KEY": "segredo"},
        },
    )
    assert generate_response.status_code == 200
    generated_files = generate_response.json()["files"]
    assert "Dockerfile" in generated_files
    assert "k8s/deployment.yaml" in generated_files
    assert "scripts/deploy.sh" in generated_files

    download_response = await client.post(
        "/download",
        json={"session_id": session_id},
    )
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/zip"
    assert download_response.content.startswith(b"PK")
