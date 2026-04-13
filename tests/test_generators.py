"""Testes para módulos do pacote generator."""

from __future__ import annotations

import pytest

from generator import dockerfile_gen, dockerignore_gen, k8s_gen


def test_dockerfile_gen_python_renderiza_sem_erro() -> None:
    context = {
        "language": "python",
        "has_build_step": False,
        "port": 8000,
        "app_module": "main:app",
    }

    content = dockerfile_gen.generate(context)

    assert "FROM python:3.12-slim" in content
    assert 'CMD ["uvicorn", "main:app"' in content
    assert "EXPOSE 8000" in content


def test_dockerfile_gen_node_build_multistage() -> None:
    context = {
        "language": "node",
        "has_build_step": True,
        "port": 3000,
        "build_output_dir": "dist",
        "start_command": "node dist/main.js",
    }

    content = dockerfile_gen.generate(context)

    assert "FROM node:20-alpine AS builder" in content
    assert "FROM node:20-alpine AS runner" in content
    assert "npm run build" in content
    assert "EXPOSE 3000" in content


def test_dockerfile_gen_linguagem_invalida() -> None:
    with pytest.raises(ValueError):
        dockerfile_gen.generate({"language": "ruby", "has_build_step": False})


def test_dockerignore_gen_python() -> None:
    content = dockerignore_gen.generate("python")
    assert ".env" in content
    assert ".venv" in content
    assert "*.pyc" in content


def test_dockerignore_gen_node() -> None:
    content = dockerignore_gen.generate("node")
    assert "node_modules" in content
    assert "npm-debug.log*" in content


def test_k8s_gen_separa_config_e_secret() -> None:
    context = {
        "app_name": "kubeforge",
        "image": "kubeforge:latest",
        "port": 8000,
        "replicas": 2,
        "health_check_path": "/health",
        "env_values": {
            "LOG_LEVEL": "info",
            "PORT": "8000",
            "DATABASE_PASSWORD": "senha",
            "API_KEY": "segredo",
        },
    }

    files = k8s_gen.generate(context)

    assert "deployment.yaml" in files
    assert "service.yaml" in files
    assert "kustomization.yaml" in files
    assert "configmap.yaml" in files
    assert "secret.yaml" in files
    assert "hpa.yaml" in files
    assert "networkpolicy.yaml" in files

    assert "LOG_LEVEL" in files["configmap.yaml"]
    assert "PORT" in files["configmap.yaml"]
    assert "DATABASE_PASSWORD" in files["secret.yaml"]
    assert "API_KEY" in files["secret.yaml"]
    assert "SUBSTITUA_ESTE_VALOR" in files["secret.yaml"]

    assert "maxReplicas: 6" in files["hpa.yaml"]
    assert "configMapRef" in files["deployment.yaml"]
    assert "secretRef" in files["deployment.yaml"]
    assert "runAsNonRoot: true" in files["deployment.yaml"]
    assert "readOnlyRootFilesystem: true" in files["deployment.yaml"]
    assert "mountPath: /tmp" in files["deployment.yaml"]
    assert "mountPath: /app/sources" in files["deployment.yaml"]
    assert "name: tmp-volume" in files["deployment.yaml"]
    assert "sizeLimit: 500Mi" in files["deployment.yaml"]
    assert "name: sources-volume" in files["deployment.yaml"]
    assert "sizeLimit: 200Mi" in files["deployment.yaml"]
    assert "configmap.yaml" in files["kustomization.yaml"]
    assert "secret.yaml" in files["kustomization.yaml"]


def test_k8s_gen_sem_hpa_e_sem_networkpolicy() -> None:
    context = {
        "app_name": "demo",
        "image": "demo:latest",
        "port": 8080,
        "replicas": 1,
        "enable_hpa": False,
        "enable_network_policy": False,
        "env_vars": ["LOG_LEVEL"],
    }

    files = k8s_gen.generate(context)

    assert "deployment.yaml" in files
    assert "service.yaml" in files
    assert "kustomization.yaml" in files
    assert "configmap.yaml" in files
    assert "secret.yaml" not in files
    assert "hpa.yaml" not in files
    assert "networkpolicy.yaml" not in files
    assert "hpa.yaml" not in files["kustomization.yaml"]
    assert "networkpolicy.yaml" not in files["kustomization.yaml"]


def test_k8s_gen_respeita_max_replicas_contexto() -> None:
    context = {
        "app_name": "demo",
        "image": "demo:latest",
        "port": 8080,
        "replicas": 2,
        "max_replicas": 10,
    }

    files = k8s_gen.generate(context)

    assert "hpa.yaml" in files
    assert "maxReplicas: 10" in files["hpa.yaml"]


def test_gera_pdb_quando_replicas_dois() -> None:
    context = {
        "app_name": "demo",
        "image": "demo:latest",
        "port": 8080,
        "replicas": 2,
    }

    files = k8s_gen.generate(context)

    assert "pdb.yaml" in files
    assert "PodDisruptionBudget" in files["pdb.yaml"]
    assert "minAvailable: 1" in files["pdb.yaml"]


def test_nao_gera_pdb_quando_replicas_um() -> None:
    context = {
        "app_name": "demo",
        "image": "demo:latest",
        "port": 8080,
        "replicas": 1,
    }

    files = k8s_gen.generate(context)

    assert "pdb.yaml" not in files
