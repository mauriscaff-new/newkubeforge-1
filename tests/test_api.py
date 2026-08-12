"""Testes assíncronos da API com httpx AsyncClient."""

from __future__ import annotations

import base64
import importlib
import io
import json
import sys
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio


def _build_unknown_zip() -> bytes:
    """Cria ZIP de projeto sem linguagem detectável."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("unknown/README.txt", "apenas texto\n")
    return buffer.getvalue()


def _kubeconfig_base64() -> str:
    """Retorna kubeconfig mínimo válido em base64 para testes."""

    kubeconfig = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters: []\n"
        "users: []\n"
        "contexts: []\n"
        "current-context: \"\"\n"
    )
    return base64.b64encode(kubeconfig.encode("utf-8")).decode("utf-8")


def _parse_json_lines(raw: str) -> list[dict[str, Any]]:
    """Converte resposta de stream em lista de objetos JSON linha a linha."""

    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        events.append(json.loads(stripped))
    return events


class _FakeSFTP:
    """Stub simples de SFTP para testes de deploy remoto."""

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, str]] = []
        self.chmod_calls: list[tuple[str, int]] = []
        self.closed = False

    def put(self, local_path: str, remote_path: str) -> None:
        """Registra uploads simulados."""

        self.put_calls.append((local_path, remote_path))

    def chmod(self, remote_path: str, mode: int) -> None:
        """Registra chmod remoto simulado."""

        self.chmod_calls.append((remote_path, mode))

    def close(self) -> None:
        """Fecha sessão SFTP simulada."""

        self.closed = True


class _FakeSSHClient:
    """Stub simples de cliente SSH para testes de deploy remoto."""

    def __init__(self) -> None:
        self.sftp_instances: list[_FakeSFTP] = []
        self.closed = False

    def open_sftp(self) -> _FakeSFTP:
        """Abre sessão SFTP simulada."""

        sftp = _FakeSFTP()
        self.sftp_instances.append(sftp)
        return sftp

    def close(self) -> None:
        """Fecha cliente SSH simulado."""

        self.closed = True


async def _create_generated_session(async_client: httpx.AsyncClient, fastapi_zip_buffer: io.BytesIO) -> str:
    """Cria sessão válida e gera artefatos para uso nos testes de deploy."""

    analyze = await async_client.post(
        "/analyze",
        data={"source_type": "zip"},
        files={"file": ("fastapi.zip", fastapi_zip_buffer.getvalue(), "application/zip")},
    )
    assert analyze.status_code == 200
    session_id = analyze.json()["session_id"]

    generated = await async_client.post(
        "/generate",
        json={"session_id": session_id, "app_name": "kubeforge", "image": "registry.io/org/kubeforge:v1"},
    )
    assert generated.status_code == 200
    return session_id


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


@pytest.mark.asyncio
async def test_deploy_remoto_exige_credencial_ssh(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
) -> None:
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

    deploy = await async_client.post(
        "/deploy/remote",
        json={
            "session_id": session_id,
            "host": "203.0.113.10",
            "username": "root",
            "remote_path": "~/kubeforge-app",
        },
    )
    assert deploy.status_code == 400
    assert "senha ou chave privada" in deploy.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fluxo_3_etapas_valida_campos_base64(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
) -> None:
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

    push_invalid_key = await async_client.post(
        "/push-to-server",
        json={
            "session_id": session_id,
            "ssh_host": "10.1.7.89",
            "ssh_user": "mscaff",
            "ssh_key": "not-base64",
        },
    )
    assert push_invalid_key.status_code == 400
    assert "ssh_key" in push_invalid_key.json()["detail"].lower()

    build_invalid_key = await async_client.post(
        "/build-image",
        json={
            "session_id": session_id,
            "ssh_host": "10.1.7.89",
            "ssh_user": "mscaff",
            "ssh_key": "not-base64",
            "image_name": "kubeforge/meu-app:latest",
        },
    )
    assert build_invalid_key.status_code == 400
    assert "ssh_key" in build_invalid_key.json()["detail"].lower()

    deploy_invalid_kubeconfig = await async_client.post(
        "/deploy",
        json={
            "session_id": session_id,
            "ssh_host": "10.1.7.89",
            "ssh_user": "mscaff",
            "ssh_key": "bm90LWEtcmVhbC1rZXk=",  # "not-a-real-key"
            "kubeconfig": "not-base64",
        },
    )
    assert deploy_invalid_kubeconfig.status_code == 400
    assert "kubeconfig" in deploy_invalid_kubeconfig.json()["detail"].lower()


@pytest.mark.asyncio
async def test_push_to_server_exige_ssh_key_ou_password(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
) -> None:
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

    response = await async_client.post(
        "/push-to-server",
        json={
            "session_id": session_id,
            "ssh_host": "10.1.7.89",
            "ssh_user": "mscaff",
            "ssh_key": None,
            "ssh_password": None,
        },
    )
    assert response.status_code == 400
    assert "ssh_key" in response.json()["detail"].lower() or "ssh_password" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_push_to_server_rejeita_git_url_invalida(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
) -> None:
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

    response = await async_client.post(
        "/push-to-server",
        json={
            "session_id": session_id,
            "ssh_host": "10.1.7.89",
            "ssh_user": "mscaff",
            "ssh_password": "dummy",
            "git_url": "http://invalido/repo.git",
        },
    )
    assert response.status_code == 400
    assert "git_url inválido" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deploy_full_stream_com_sessao_invalida(async_client: httpx.AsyncClient) -> None:
    kubeconfig_b64 = base64.b64encode(b"apiVersion: v1\n").decode("utf-8")
    response = await async_client.post(
        "/deploy-full",
        json={
            "session_id": "sessao-invalida",
            "git_url": "https://github.com/octocat/Hello-World.git",
            "app_name": "meu-app",
            "tag": "latest",
            "kubeconfig": kubeconfig_b64,
        },
    )
    assert response.status_code == 200
    assert "[ERRO" in response.text


@pytest.mark.asyncio
async def test_deploy_from_git_fluxo_completo_com_registry(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _create_generated_session(async_client, fastapi_zip_buffer)

    import main as main_module

    ssh_client = _FakeSSHClient()
    executed_commands: list[str] = []

    def fake_open_ssh_client(*_args: Any, **_kwargs: Any) -> _FakeSSHClient:
        return ssh_client

    def fake_upload_tree_via_sftp(_ssh: Any, _local_root: Path, _remote_root: str) -> None:
        return None

    def fake_exec_ssh_command(_ssh: Any, command: str, timeout: int) -> tuple[int, str, str]:
        _ = timeout
        executed_commands.append(command)
        if "git clone --depth 1" in command:
            return 0, "clone ok", ""
        if "docker build -t" in command:
            return 0, "build ok", ""
        if "docker login" in command:
            return 0, "login ok", ""
        if "docker push" in command:
            return 0, "push ok", ""
        if "kubectl apply -k k8s/" in command:
            return 0, "apply ok", ""
        if command.startswith("rm -f "):
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(main_module, "_open_ssh_client", fake_open_ssh_client)
    monkeypatch.setattr(main_module, "_upload_tree_via_sftp", fake_upload_tree_via_sftp)
    monkeypatch.setattr(main_module, "_exec_ssh_command", fake_exec_ssh_command)

    response = await async_client.post(
        "/deploy/from-git",
        json={
            "session_id": session_id,
            "git_url": "https://github.com/octocat/Hello-World.git",
            "git_branch": "main",
            "ssh_host": "203.0.113.10",
            "ssh_user": "root",
            "ssh_password": "dummy-password",
            "image_name": "registry.io/org/kubeforge:v1",
            "kubeconfig": _kubeconfig_base64(),
            "registry_url": "registry.io",
            "registry_user": "robot",
            "registry_password": "robot-pass",
        },
    )

    assert response.status_code == 200
    events = _parse_json_lines(response.text)
    assert events[-1]["step"] == "finished"
    assert events[-1]["status"] == "success"
    assert any(event["step"] == "push" and event["status"] == "done" for event in events)
    assert any("docker login" in command for command in executed_commands)
    assert any("docker push" in command for command in executed_commands)
    assert ssh_client.sftp_instances
    assert any(
        remote_path.startswith("/tmp/kubeforge-kubeconfig-")
        for sftp in ssh_client.sftp_instances
        for _local_path, remote_path in sftp.put_calls
    )


@pytest.mark.asyncio
async def test_deploy_from_git_sem_registry_pula_push(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _create_generated_session(async_client, fastapi_zip_buffer)

    import main as main_module

    ssh_client = _FakeSSHClient()
    executed_commands: list[str] = []

    def fake_open_ssh_client(*_args: Any, **_kwargs: Any) -> _FakeSSHClient:
        return ssh_client

    def fake_upload_tree_via_sftp(_ssh: Any, _local_root: Path, _remote_root: str) -> None:
        return None

    def fake_exec_ssh_command(_ssh: Any, command: str, timeout: int) -> tuple[int, str, str]:
        _ = timeout
        executed_commands.append(command)
        if "git clone --depth 1" in command:
            return 0, "clone ok", ""
        if "docker build -t" in command:
            return 0, "build ok", ""
        if "kubectl apply -k k8s/" in command:
            return 0, "apply ok", ""
        if command.startswith("rm -f "):
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(main_module, "_open_ssh_client", fake_open_ssh_client)
    monkeypatch.setattr(main_module, "_upload_tree_via_sftp", fake_upload_tree_via_sftp)
    monkeypatch.setattr(main_module, "_exec_ssh_command", fake_exec_ssh_command)

    response = await async_client.post(
        "/deploy/from-git",
        json={
            "session_id": session_id,
            "git_url": "https://github.com/octocat/Hello-World.git",
            "ssh_host": "203.0.113.10",
            "ssh_user": "root",
            "ssh_password": "dummy-password",
            "image_name": "registry.io/org/kubeforge:v1",
            "kubeconfig": _kubeconfig_base64(),
        },
    )

    assert response.status_code == 200
    events = _parse_json_lines(response.text)
    assert any(event["step"] == "push" and event["status"] == "skipped" for event in events)
    assert events[-1]["step"] == "finished"
    assert events[-1]["status"] == "success"
    assert all("docker login" not in command for command in executed_commands)
    assert all("docker push" not in command for command in executed_commands)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_step", "expected_completed_steps"),
    [
        ("clone", []),
        ("upload", ["clone"]),
        ("build", ["clone", "upload"]),
        ("push", ["clone", "upload", "build"]),
        ("apply", ["clone", "upload", "build", "push"]),
    ],
)
async def test_deploy_from_git_falha_por_etapa(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
    monkeypatch: pytest.MonkeyPatch,
    failed_step: str,
    expected_completed_steps: list[str],
) -> None:
    session_id = await _create_generated_session(async_client, fastapi_zip_buffer)

    import main as main_module

    ssh_client = _FakeSSHClient()

    def fake_open_ssh_client(*_args: Any, **_kwargs: Any) -> _FakeSSHClient:
        return ssh_client

    def fake_upload_tree_via_sftp(_ssh: Any, _local_root: Path, _remote_root: str) -> None:
        if failed_step == "upload":
            raise RuntimeError("falha no upload")

    def fake_exec_ssh_command(_ssh: Any, command: str, timeout: int) -> tuple[int, str, str]:
        _ = timeout
        if failed_step == "clone" and "git clone --depth 1" in command:
            return 1, "", "clone failed"
        if failed_step == "build" and "docker build -t" in command:
            return 1, "", "build failed"
        if failed_step == "push" and "docker push" in command:
            return 1, "", "push failed"
        if failed_step == "apply" and "kubectl apply -k k8s/" in command:
            return 1, "", "apply failed"
        if command.startswith("rm -f "):
            return 0, "", ""
        return 0, "ok", ""

    monkeypatch.setattr(main_module, "_open_ssh_client", fake_open_ssh_client)
    monkeypatch.setattr(main_module, "_upload_tree_via_sftp", fake_upload_tree_via_sftp)
    monkeypatch.setattr(main_module, "_exec_ssh_command", fake_exec_ssh_command)

    response = await async_client.post(
        "/deploy/from-git",
        json={
            "session_id": session_id,
            "git_url": "https://github.com/octocat/Hello-World.git",
            "ssh_host": "203.0.113.10",
            "ssh_user": "root",
            "ssh_password": "dummy-password",
            "image_name": "registry.io/org/kubeforge:v1",
            "kubeconfig": _kubeconfig_base64(),
            "registry_url": "registry.io",
            "registry_user": "robot",
            "registry_password": "robot-pass",
        },
    )

    assert response.status_code == 200
    events = _parse_json_lines(response.text)
    assert any(event["step"] == failed_step and event["status"] == "error" for event in events)

    finished = events[-1]
    assert finished["step"] == "finished"
    assert finished["status"] == "failed"
    assert finished["failed_at"] == failed_step
    assert finished["completed_steps"] == expected_completed_steps


@pytest.mark.asyncio
async def test_deploy_from_git_valida_campos_obrigatorios(
    async_client: httpx.AsyncClient,
    fastapi_zip_buffer: io.BytesIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _create_generated_session(async_client, fastapi_zip_buffer)

    import main as main_module

    open_ssh_called = {"value": False}

    def fail_if_open_ssh_called(*_args: Any, **_kwargs: Any) -> Any:
        open_ssh_called["value"] = True
        raise AssertionError("Não deveria abrir SSH em validação inválida.")

    monkeypatch.setattr(main_module, "_open_ssh_client", fail_if_open_ssh_called)

    missing_ssh = await async_client.post(
        "/deploy/from-git",
        json={
            "session_id": session_id,
            "git_url": "https://github.com/octocat/Hello-World.git",
            "ssh_host": "203.0.113.10",
            "ssh_user": "root",
            "ssh_key": None,
            "ssh_password": None,
            "image_name": "registry.io/org/kubeforge:v1",
            "kubeconfig": _kubeconfig_base64(),
        },
    )
    assert missing_ssh.status_code == 400
    assert "ssh_key" in missing_ssh.json()["detail"].lower() or "ssh_password" in missing_ssh.json()["detail"].lower()

    missing_registry_creds = await async_client.post(
        "/deploy/from-git",
        json={
            "session_id": session_id,
            "git_url": "https://github.com/octocat/Hello-World.git",
            "ssh_host": "203.0.113.10",
            "ssh_user": "root",
            "ssh_password": "dummy-password",
            "image_name": "registry.io/org/kubeforge:v1",
            "kubeconfig": _kubeconfig_base64(),
            "registry_url": "registry.io",
        },
    )
    assert missing_registry_creds.status_code == 400
    assert "registry_user" in missing_registry_creds.json()["detail"].lower()
    assert open_ssh_called["value"] is False
