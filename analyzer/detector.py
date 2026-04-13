"""Deteccao de linguagem e framework do projeto."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

IGNORED_DIRS: set[str] = {"node_modules", "__pycache__", ".git", "dist", "build", "target"}
MAX_WALK_DEPTH = 3


def _read_text_safe(path: Path) -> str:
    """Le o arquivo como texto sem propagar erros de decoding."""

    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _iter_files(root: Path) -> list[Path]:
    """Lista arquivos do projeto com limite de profundidade e diretorios ignorados."""

    collected: list[Path] = []
    for current_root, dirs, files in os.walk(root, topdown=True):
        current_path = Path(current_root)
        depth = len(current_path.relative_to(root).parts)

        dirs[:] = [directory for directory in dirs if directory not in IGNORED_DIRS]
        if depth >= MAX_WALK_DEPTH:
            dirs[:] = []

        for filename in files:
            collected.append(current_path / filename)
    return collected


def _parse_package_json(content: str) -> dict[str, Any]:
    """Converte package.json em dict de forma resiliente."""

    if not content.strip():
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _detect_node(files_by_name: dict[str, Path]) -> dict[str, Any] | None:
    """Detecta projeto Node.js e framework relacionado."""

    package_json = files_by_name.get("package.json")
    if package_json is None:
        return None

    raw_package = _parse_package_json(_read_text_safe(package_json))
    deps: dict[str, Any] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        maybe_map = raw_package.get(section, {})
        if isinstance(maybe_map, dict):
            deps.update(maybe_map)

    dep_names = set(deps.keys())
    scripts = raw_package.get("scripts", {})
    has_build_script = isinstance(scripts, dict) and bool(scripts.get("build"))

    framework = "node"
    has_build_step = has_build_script

    if "next" in dep_names:
        framework = "nextjs"
        has_build_step = True
    elif "@nestjs/core" in dep_names:
        framework = "nestjs"
        has_build_step = True
    elif "fastify" in dep_names:
        framework = "fastify"
    elif "express" in dep_names:
        framework = "express"

    build_tool = "npm"
    if "pnpm-lock.yaml" in files_by_name:
        build_tool = "pnpm"
    elif "yarn.lock" in files_by_name:
        build_tool = "yarn"

    return {
        "language": "node",
        "framework": framework,
        "build_tool": build_tool,
        "has_build_step": has_build_step,
    }


def _detect_python(files_by_name: dict[str, Path]) -> dict[str, Any] | None:
    """Detecta projeto Python e framework relacionado."""

    indicators = {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}
    if not any(name in files_by_name for name in indicators):
        return None

    combined_text = "\n".join(
        _read_text_safe(files_by_name[name]) for name in ("pyproject.toml", "requirements.txt", "setup.py") if name in files_by_name
    ).lower()

    framework = "python"
    if "fastapi" in combined_text:
        framework = "fastapi"
    elif "django" in combined_text:
        framework = "django"
    elif "flask" in combined_text:
        framework = "flask"

    build_tool = "pip"
    pyproject = files_by_name.get("pyproject.toml")
    if pyproject is not None:
        pyproject_text = _read_text_safe(pyproject).lower()
        if "[tool.poetry" in pyproject_text:
            build_tool = "poetry"
        else:
            build_tool = "uv"

    return {
        "language": "python",
        "framework": framework,
        "build_tool": build_tool,
        "has_build_step": False,
    }


def _detect_java(files_by_name: dict[str, Path]) -> dict[str, Any] | None:
    """Detecta projeto Java (Maven ou Gradle)."""

    pom = files_by_name.get("pom.xml")
    gradle = files_by_name.get("build.gradle") or files_by_name.get("build.gradle.kts")

    if pom is None and gradle is None:
        return None

    if pom is not None:
        content = _read_text_safe(pom).lower()
        framework = "spring" if "spring-boot" in content else "java"
        return {
            "language": "java",
            "framework": framework,
            "build_tool": "maven",
            "has_build_step": True,
        }

    gradle_content = _read_text_safe(gradle).lower() if gradle is not None else ""
    framework = "spring" if "org.springframework.boot" in gradle_content or "spring-boot" in gradle_content else "java"
    return {
        "language": "java",
        "framework": framework,
        "build_tool": "gradle",
        "has_build_step": True,
    }


def _detect_go(files_by_name: dict[str, Path]) -> dict[str, Any] | None:
    """Detecta projeto Go."""

    go_mod = files_by_name.get("go.mod")
    if go_mod is None:
        return None

    return {
        "language": "go",
        "framework": "go",
        "build_tool": "go",
        "has_build_step": True,
    }


def _detect_dotnet(files: list[Path]) -> dict[str, Any] | None:
    """Detecta projeto .NET."""

    has_csproj = any(path.suffix.lower() == ".csproj" for path in files)
    has_sln = any(path.suffix.lower() == ".sln" for path in files)
    if not (has_csproj or has_sln):
        return None

    return {
        "language": "dotnet",
        "framework": "dotnet",
        "build_tool": "dotnet",
        "has_build_step": True,
    }


def detect(project_path: str) -> dict[str, Any]:
    """Detecta linguagem e framework principais de um projeto.

    Args:
        project_path: Caminho absoluto ou relativo da raiz do projeto.

    Returns:
        Dicionario com `language`, `framework`, `build_tool` e `has_build_step`.
    """

    root = Path(project_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("project_path deve apontar para um diretorio existente.")

    files = _iter_files(root)
    files_by_name = {path.name: path for path in files}

    detectors = (
        _detect_node(files_by_name),
        _detect_python(files_by_name),
        _detect_java(files_by_name),
        _detect_go(files_by_name),
        _detect_dotnet(files),
    )

    for result in detectors:
        if result is not None:
            return result

    return {
        "language": "unknown",
        "framework": "unknown",
        "build_tool": "unknown",
        "has_build_step": False,
    }
