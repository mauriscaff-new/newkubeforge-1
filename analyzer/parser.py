"""Parser de configuracoes do projeto para fase de geracao."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from analyzer.rules import FRAMEWORK_OVERRIDES, LANGUAGE_DEFAULTS

ENV_VAR_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
PORT_INLINE_PATTERN = re.compile(r"(?:(?:--port|-p)\s*=?\s*|PORT\s*=\s*)(\d{2,5})", re.IGNORECASE)


def _read_text_safe(path: Path) -> str:
    """Le texto de arquivo sem levantar erro para arquivos ausentes/invalidos."""

    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _merge_defaults(language: str, framework: str) -> dict[str, Any]:
    """Une defaults por linguagem com overrides de framework."""

    merged: dict[str, Any] = {}
    merged.update(LANGUAGE_DEFAULTS.get(language, {}))
    merged.update(FRAMEWORK_OVERRIDES.get(framework, {}))
    return merged


def _to_int(value: Any, default: int) -> int:
    """Converte valor para int com fallback seguro."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_env_vars(project_root: Path) -> tuple[list[str], dict[str, str]]:
    """Extrai nomes/valores de variaveis de `.env` e `.env.example`."""

    env_vars: set[str] = set()
    env_values: dict[str, str] = {}
    for filename in (".env", ".env.example"):
        path = project_root / filename
        if not path.exists():
            continue
        for line in _read_text_safe(path).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = ENV_VAR_PATTERN.match(stripped)
            if not match:
                continue
            key = match.group(1).strip()
            value = match.group(2).strip().strip('"').strip("'")
            env_vars.add(key)
            if key not in env_values:
                env_values[key] = value
    return sorted(env_vars), env_values


def _detect_port_from_texts(texts: list[str]) -> int | None:
    """Busca porta em textos de comando/script."""

    for text in texts:
        match = PORT_INLINE_PATTERN.search(text)
        if match is None:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            continue
    return None


def _port_from_env_values(env_values: dict[str, str]) -> int | None:
    """Retorna porta vinda de variavel de ambiente, quando presente."""

    for key in ("PORT", "APP_PORT", "SERVER_PORT"):
        raw = env_values.get(key)
        if raw is None or not raw.isdigit():
            continue
        return int(raw)
    return None


def _parse_package_json(path: Path) -> dict[str, Any]:
    """Converte package.json em dict de forma tolerante a erro."""

    content = _read_text_safe(path)
    if not content.strip():
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_node(project_root: Path, framework: str, defaults: dict[str, Any], env_values: dict[str, str]) -> tuple[int, str]:
    """Resolve porta e comando de start para projetos Node."""

    package_json_data = _parse_package_json(project_root / "package.json")
    scripts = package_json_data.get("scripts", {})
    scripts = scripts if isinstance(scripts, dict) else {}

    script_port = _detect_port_from_texts([str(command) for command in scripts.values()])
    env_port = _port_from_env_values(env_values)
    default_port = _to_int(defaults.get("port", 3000), 3000)
    port = script_port or env_port or default_port

    start_command = str(defaults.get("start_command", "npm run start"))
    if "start" in scripts:
        start_command = "npm run start"
    elif framework == "nestjs" and "start:prod" in scripts:
        start_command = "npm run start:prod"
    elif "dev" in scripts:
        start_command = "npm run dev"
    elif framework == "nextjs":
        start_command = f"next start -p {port}"
    elif "main" in package_json_data:
        start_command = f"node {package_json_data['main']}"

    return port, start_command


def _read_python_dependency_text(project_root: Path) -> str:
    """Agrega conteudo de requirements.txt e pyproject.toml."""

    requirements = _read_text_safe(project_root / "requirements.txt")
    pyproject_text = _read_text_safe(project_root / "pyproject.toml")
    return f"{requirements}\n{pyproject_text}".lower()


def _parse_python(project_root: Path, framework: str, defaults: dict[str, Any], env_values: dict[str, str]) -> tuple[int, str]:
    """Resolve porta e comando de start para projetos Python."""

    dependency_text = _read_python_dependency_text(project_root)
    inferred_framework = framework
    if inferred_framework in ("unknown", "", "python"):
        if "fastapi" in dependency_text:
            inferred_framework = "fastapi"
        elif "django" in dependency_text:
            inferred_framework = "django"
        elif "flask" in dependency_text:
            inferred_framework = "flask"

    env_port = _port_from_env_values(env_values)
    framework_port = FRAMEWORK_OVERRIDES.get(inferred_framework, {}).get("port")
    default_port = _to_int(framework_port if framework_port is not None else defaults.get("port", 8000), 8000)
    port = env_port or default_port

    if inferred_framework == "fastapi":
        return port, f"uvicorn main:app --host 0.0.0.0 --port {port}"
    if inferred_framework == "django":
        return port, f"python manage.py runserver 0.0.0.0:{port}"
    if inferred_framework == "flask":
        return port, f"flask run --host 0.0.0.0 --port {port}"
    return port, str(defaults.get("start_command", "python main.py"))


def _detect_java_build_tool(project_root: Path) -> str:
    """Retorna ferramenta de build Java."""

    if (project_root / "pom.xml").exists():
        return "maven"
    if (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        return "gradle"
    return "maven"


def _parse_pyproject(project_root: Path) -> dict[str, Any]:
    """Le pyproject.toml com parser TOML nativo."""

    path = project_root / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse(project_path: str, language: str, framework: str) -> dict[str, Any]:
    """Extrai configuracoes basicas do projeto para geracao.

    Args:
        project_path: Caminho da raiz do projeto.
        language: Linguagem detectada.
        framework: Framework detectado.

    Returns:
        Dicionario com `port`, `start_command` e `env_vars`.
    """

    project_root = Path(project_path).expanduser().resolve()
    if not project_root.exists() or not project_root.is_dir():
        raise ValueError("project_path deve apontar para um diretorio existente.")

    defaults = _merge_defaults(language, framework)
    env_vars, env_values = _read_env_vars(project_root)

    if language == "node":
        port, start_command = _parse_node(project_root, framework, defaults, env_values)
    elif language == "python":
        _ = _parse_pyproject(project_root)
        port, start_command = _parse_python(project_root, framework, defaults, env_values)
    elif language == "java":
        env_port = _port_from_env_values(env_values)
        build_tool = _detect_java_build_tool(project_root)
        if framework == "springboot_gradle" or (framework == "springboot" and build_tool == "gradle"):
            port = env_port or _to_int(FRAMEWORK_OVERRIDES["springboot_gradle"].get("port"), 8080)
            start_command = str(FRAMEWORK_OVERRIDES["springboot_gradle"]["start_command"])
        elif framework in ("springboot", "spring"):
            port = env_port or _to_int(FRAMEWORK_OVERRIDES["springboot"].get("port"), 8080)
            start_command = str(FRAMEWORK_OVERRIDES["springboot"]["start_command"])
        else:
            port = env_port or _to_int(defaults.get("port", 8080), 8080)
            start_command = str(defaults.get("start_command", "java -jar app.jar"))
    elif language == "go":
        port = _port_from_env_values(env_values) or _to_int(defaults.get("port", 8080), 8080)
        start_command = str(defaults.get("start_command", "./app"))
    elif language == "dotnet":
        port = _port_from_env_values(env_values) or _to_int(defaults.get("port", 8080), 8080)
        start_command = str(defaults.get("start_command", "dotnet run"))
    else:
        port = _port_from_env_values(env_values) or 8000
        start_command = "python main.py"

    return {
        "port": int(port),
        "start_command": start_command,
        "env_vars": env_vars,
    }
