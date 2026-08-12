"""Testes assíncronos da API com httpx AsyncClient."""

from __future__ import annotations

import importlib
import io
import sys
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio


def _build_unknown_zip() -> bytes:
    """Cria ZIP de projeto sem linguagem detectável."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("unknown/README.txt", "apenas texto\n")
    return buffer.getvalue()


@pytest_asyncio.fixture
async def async_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Cliente HTTP assíncrono para chamadas ASGI locais."""

    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000")

    project_root = Path(__file__).resolve().parents[1]
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

    sys.modules.pop("main", None)
    main_module = importlib.import_module("main")
    main_module = importlib.reload(main_module)

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_analyze_zip_fastapi(async_client: httpx.AsyncClient, fastapi_zip_buffer: io.BytesIO) -> None:
    response = await async_client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("fastapi.zip", fastapi_zip_buffer.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["analysis"]["language"] == "python"
    assert body["analysis"]["framework"] == "fastapi"
    assert body["analysis"]["port"] == 8000


@pytest.mark.asyncio
async def test_generate_payload_completo(async_client: httpx.AsyncClient, fastapi_zip_buffer: io.BytesIO) -> None:
    analyze = await async_client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("fastapi.zip", fastapi_zip_buffer.getvalue(), "application/zip")},
    )
    assert analyze.status_code == 200
    session_id = analyze.json()["session_id"]

    payload = {
        "session_id": session_id,
        "app_name": "kubeforge",
        "image": "ghcr.io/mauriscaff/kubeforge:latest",
        "namespace": "kubeforge-system",
        "service_type": "ClusterIP",
        "replicas": 2,
        "port": 8000,
        "max_replicas": 6,
        "health_check_path": "/health",
        "env": {"LOG_LEVEL": "info", "API_KEY": "nao-usar-em-prod"},
        "resources": {
            "requests": {"cpu": "250m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
        "enable_hpa": True,
        "enable_network_policy": True,
    }
    generated = await async_client.post("/generate", json=payload)

    assert generated.status_code == 200
    body = generated.json()
    assert "Dockerfile" in body["files"]
    assert "k8s/deployment.yaml" in body["files"]
    assert "scripts/deploy.sh" in body["files"]
    assert "file_contents" in body
    assert "Dockerfile" in body["file_contents"]
    assert "apiVersion: apps/v1" in body["file_contents"]["k8s/deployment.yaml"]


@pytest.mark.asyncio
async def test_download_zip_valido(async_client: httpx.AsyncClient, fastapi_zip_buffer: io.BytesIO) -> None:
    analyze = await async_client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("fastapi.zip", fastapi_zip_buffer.getvalue(), "application/zip")},
    )
    assert analyze.status_code == 200
    session_id = analyze.json()["session_id"]

    generated = await async_client.post(
        "/generate",
        json={"session_id": session_id, "app_name": "kubeforge", "image": "repo/kubeforge:latest"},
    )
    assert generated.status_code == 200

    download = await async_client.post("/download", json={"session_id": session_id})
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = archive.namelist()
    assert "Dockerfile" in names
    assert "k8s/deployment.yaml" in names


@pytest.mark.asyncio
async def test_erro_zip_invalido(async_client: httpx.AsyncClient) -> None:
    response = await async_client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("invalid.zip", b"not-a-valid-zip", "application/zip")},
    )

    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]


@pytest.mark.asyncio
async def test_erro_linguagem_nao_detectada(async_client: httpx.AsyncClient) -> None:
    unknown_zip = _build_unknown_zip()
    response = await async_client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("unknown.zip", unknown_zip, "application/zip")},
    )

    assert response.status_code == 422
    assert "não foi possível detectar a linguagem" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_erro_session_id_invalido(async_client: httpx.AsyncClient) -> None:
    response = await async_client.post("/generate", json={"session_id": "sessao-invalida"})

    assert response.status_code == 404
    assert "inválida" in response.json()["detail"].lower() or "expirada" in response.json()["detail"].lower()
