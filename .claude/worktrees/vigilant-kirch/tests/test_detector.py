"""Testes para detector de linguagem e framework."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyzer.detector import detect


def _write(path: Path, content: str) -> None:
    """Cria arquivo de texto garantindo diretorio pai."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_detect_node_nextjs(tmp_path: Path) -> None:
    package_json = {
        "dependencies": {"next": "14.0.0", "react": "18.0.0"},
        "scripts": {"build": "next build"},
    }
    _write(tmp_path / "package.json", json.dumps(package_json))
    _write(tmp_path / "package-lock.json", "{}")

    result = detect(str(tmp_path))

    assert result["language"] == "node"
    assert result["framework"] == "nextjs"
    assert result["build_tool"] == "npm"
    assert result["has_build_step"] is True


def test_detect_node_nestjs_com_yarn(tmp_path: Path) -> None:
    package_json = {
        "dependencies": {"@nestjs/core": "11.0.0"},
        "scripts": {"start": "node dist/main.js"},
    }
    _write(tmp_path / "package.json", json.dumps(package_json))
    _write(tmp_path / "yarn.lock", "# lock")

    result = detect(str(tmp_path))

    assert result["language"] == "node"
    assert result["framework"] == "nestjs"
    assert result["build_tool"] == "yarn"
    assert result["has_build_step"] is True


def test_detect_python_fastapi(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "fastapi==0.111.0\nuvicorn==0.30.0\n")

    result = detect(str(tmp_path))

    assert result["language"] == "python"
    assert result["framework"] == "fastapi"
    assert result["build_tool"] == "pip"
    assert result["has_build_step"] is False


def test_detect_java_maven_spring(tmp_path: Path) -> None:
    _write(tmp_path / "pom.xml", "<dependency>spring-boot-starter-web</dependency>")

    result = detect(str(tmp_path))

    assert result["language"] == "java"
    assert result["framework"] == "spring"
    assert result["build_tool"] == "maven"
    assert result["has_build_step"] is True


def test_detect_java_gradle_spring(tmp_path: Path) -> None:
    _write(tmp_path / "build.gradle.kts", 'id("org.springframework.boot") version "3.2.0"')

    result = detect(str(tmp_path))

    assert result["language"] == "java"
    assert result["framework"] == "spring"
    assert result["build_tool"] == "gradle"
    assert result["has_build_step"] is True


def test_detect_go(tmp_path: Path) -> None:
    _write(tmp_path / "go.mod", "module github.com/example/app\n")

    result = detect(str(tmp_path))

    assert result["language"] == "go"
    assert result["framework"] == "go"
    assert result["build_tool"] == "go"
    assert result["has_build_step"] is True


def test_detect_dotnet(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "App.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>")

    result = detect(str(tmp_path))

    assert result["language"] == "dotnet"
    assert result["framework"] == "dotnet"
    assert result["build_tool"] == "dotnet"
    assert result["has_build_step"] is True


def test_ignore_dirs_e_limite_profundidade(tmp_path: Path) -> None:
    _write(tmp_path / "node_modules" / "package.json", '{"dependencies":{"express":"5"}}')
    _write(
        tmp_path / "l1" / "l2" / "l3" / "l4" / "package.json",
        '{"dependencies":{"next":"14"}}',
    )

    result = detect(str(tmp_path))

    assert result["language"] == "unknown"
    assert result["framework"] == "unknown"
    assert result["build_tool"] == "unknown"
    assert result["has_build_step"] is False


def test_erro_quando_caminho_invalido(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        detect(str(tmp_path / "nao-existe"))
