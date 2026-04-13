"""Testes para parser e regras."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyzer.parser import parse
from analyzer.rules import FRAMEWORK_OVERRIDES, K8S_DEFAULTS, LANGUAGE_DEFAULTS


def _write(path: Path, content: str) -> None:
    """Cria arquivo de texto garantindo diretorios pais."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_parse_node_port_e_start_command(tmp_path: Path) -> None:
    package = {
        "scripts": {
            "start": "next start --port 4123",
            "build": "next build",
        },
        "dependencies": {"next": "14.0.0"},
    }
    _write(tmp_path / "package.json", json.dumps(package))
    _write(tmp_path / ".env.example", "NEXT_PUBLIC_API_URL=http://localhost:8000\nPORT=3000\n")

    result = parse(str(tmp_path), language="node", framework="nextjs")

    assert result["port"] == 4123
    assert result["start_command"] == "npm run start"
    assert "NEXT_PUBLIC_API_URL" in result["env_vars"]
    assert "PORT" in result["env_vars"]


def test_parse_python_fastapi_com_port_do_env(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "fastapi==0.111.0\nuvicorn==0.30.0\n")
    _write(tmp_path / ".env", "PORT=9001\nSECRET_KEY=abc\n")

    result = parse(str(tmp_path), language="python", framework="fastapi")

    assert result["port"] == 9001
    assert result["start_command"] == "uvicorn main:app --host 0.0.0.0 --port 9001"
    assert result["env_vars"] == ["PORT", "SECRET_KEY"]


def test_parse_python_detecta_framework_pelo_pyproject(tmp_path: Path) -> None:
    pyproject = """
[project]
name = "demo"
version = "0.1.0"
dependencies = ["flask>=3.0.0"]
"""
    _write(tmp_path / "pyproject.toml", pyproject)

    result = parse(str(tmp_path), language="python", framework="unknown")

    assert result["port"] == 5000
    assert result["start_command"] == "flask run --host 0.0.0.0 --port 5000"


def test_parse_java_springboot_gradle(tmp_path: Path) -> None:
    _write(tmp_path / "build.gradle.kts", 'id("org.springframework.boot") version "3.3.0"')
    _write(tmp_path / ".env.example", "SESSION_TTL_MINUTES=30\n")

    result = parse(str(tmp_path), language="java", framework="springboot_gradle")

    assert result["port"] == 8080
    assert result["start_command"] == "./gradlew bootRun"
    assert result["env_vars"] == ["SESSION_TTL_MINUTES"]


def test_parse_unknown(tmp_path: Path) -> None:
    result = parse(str(tmp_path), language="unknown", framework="unknown")

    assert result["port"] == 8000
    assert result["start_command"] == "python main.py"
    assert result["env_vars"] == []


def test_parse_erro_quando_diretorio_invalido(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        parse(str(tmp_path / "inexistente"), language="python", framework="fastapi")


def test_rules_language_defaults_tem_alpine() -> None:
    for language in ("node", "python", "java", "go", "dotnet"):
        assert language in LANGUAGE_DEFAULTS
        base_image = str(LANGUAGE_DEFAULTS[language]["base_image"])
        assert "alpine" in base_image


def test_rules_framework_overrides_obrigatorios() -> None:
    expected = {
        "nextjs",
        "nestjs",
        "fastapi",
        "django",
        "flask",
        "springboot",
        "springboot_gradle",
    }
    assert expected.issubset(FRAMEWORK_OVERRIDES.keys())
    assert K8S_DEFAULTS["service_type"] == "ClusterIP"
