"""Testes da API principal do KubeForge."""

from __future__ import annotations

import importlib
import io
import sys
import zipfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000")

    project_root = Path(__file__).resolve().parents[1]
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

    sys.modules.pop("main", None)
    main_module = importlib.import_module("main")
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as test_client:
        yield test_client


def test_root_e_health(client: TestClient) -> None:
    root = client.get("/")
    health = client.get("/health")

    assert root.status_code == 200
    assert root.json()["name"] == "KubeForge API"
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_rejeita_git_url_invalido(client: TestClient) -> None:
    response = client.post(
        "/analyze",
        json={"source_type": "git", "git_url": "http://example.com/repo.git"},
    )

    assert response.status_code == 400
    assert "git_url inválido" in response.json()["detail"]


def test_aceita_folder_no_analyze(client: TestClient, sandbox_source: Path) -> None:
    response = client.post(
        "/analyze",
        json={"source_type": "folder", "source_value": str(sandbox_source)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["analysis"]["language"] == "python"
    assert body["analysis"]["framework"] == "fastapi"


def test_aceita_zip_no_analyze(client: TestClient) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project/requirements.txt", "flask==3.0.0\n")
        archive.writestr("project/app.py", "print('ok')\n")
    buffer.seek(0)

    response = client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("project.zip", buffer.read(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["analysis"]["language"] == "python"


def test_aplica_rate_limit_no_analyze(client: TestClient, sandbox_source: Path) -> None:
    payload = {"source_type": "folder", "source_value": str(sandbox_source)}

    for _ in range(10):
        response = client.post("/analyze", json=payload)
        assert response.status_code == 200

    limited_response = client.post("/analyze", json=payload)
    assert limited_response.status_code == 429


def test_validacoes_generate_request(client: TestClient) -> None:
    replicas_invalid = client.post(
        "/generate",
        json={"session_id": "abc", "replicas": 0},
    )
    port_invalid = client.post(
        "/generate",
        json={"session_id": "abc", "port": 70000},
    )

    assert replicas_invalid.status_code == 422
    assert port_invalid.status_code == 422


def test_fluxo_generate_e_download(client: TestClient, sandbox_source: Path) -> None:
    analyze_response = client.post(
        "/analyze",
        json={"source_type": "folder", "source_value": str(sandbox_source)},
    )
    assert analyze_response.status_code == 200
    session_id = analyze_response.json()["session_id"]

    generate_response = client.post(
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

    download_response = client.post(
        "/download",
        json={"session_id": session_id},
    )
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/zip"
    assert download_response.content.startswith(b"PK")
