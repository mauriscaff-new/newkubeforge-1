"""Gerador de Dockerfile a partir de templates Jinja2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _split_filter(value: str, separator: str = ",") -> list[str]:
    """Quebra string pelo separador e remove espaços vazios."""

    return [item.strip() for item in value.split(separator) if item.strip()]


def _build_environment() -> Environment:
    """Cria ambiente Jinja2 com validação estrita de variáveis."""

    templates_root = Path(__file__).resolve().parents[1] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["split"] = _split_filter
    return environment


# Versoes usadas quando o projeto nao declara "packageManager".
# Sem pin, "corepack enable" instala a versao mais recente, que pode exigir
# um Node mais novo que o da imagem base e quebrar o build.
DEFAULT_COREPACK_VERSIONS: dict[str, str] = {
    "pnpm": "9.15.4",
    "yarn": "1.22.22",
}


def _resolve_corepack_spec(build_tool: str, package_manager: str) -> str:
    """Resolve o especificador `nome@versao` usado por `corepack prepare`.

    Args:
        build_tool: Gerenciador detectado (`yarn` ou `pnpm`).
        package_manager: Valor do campo `packageManager` do package.json.

    Returns:
        Especificador com versao fixa, ex.: `pnpm@9.15.4`.
    """

    declared = package_manager.strip()
    # O campo pode vir com hash de integridade ("pnpm@9.15.4+sha512..."),
    # que o corepack aceita apenas na forma nome@versao.
    if "+" in declared:
        declared = declared.split("+", 1)[0]
    if declared.startswith(f"{build_tool}@") and declared != f"{build_tool}@":
        return declared
    return f"{build_tool}@{DEFAULT_COREPACK_VERSIONS[build_tool]}"


def node_package_manager_context(build_tool: str, has_lockfile: bool, package_manager: str = "") -> dict[str, Any]:
    """Resolve comandos de instalacao Node conforme gerenciador e lockfile.

    Instalacoes reprodutiveis (`npm ci`, `--frozen-lockfile`) exigem lockfile.
    Sem lockfile no projeto, o comando degrada para a instalacao normal, que
    ainda funciona em vez de quebrar o build da imagem.

    Args:
        build_tool: Gerenciador detectado (`npm`, `yarn` ou `pnpm`).
        has_lockfile: Indica se o projeto versiona o lockfile correspondente.
        package_manager: Campo `packageManager` do package.json, quando existir.

    Returns:
        Dicionario com comandos e flags usados pelos templates Node.
    """

    normalized = build_tool.strip().lower()

    if normalized == "pnpm":
        return {
            "pm_needs_corepack": True,
            "pm_corepack_spec": _resolve_corepack_spec("pnpm", package_manager),
            "pm_copy_manifests": "package.json pnpm-lock.yaml*",
            "pm_install_all": "pnpm install --frozen-lockfile" if has_lockfile else "pnpm install",
            "pm_install_prod": ("pnpm install --frozen-lockfile --prod" if has_lockfile else "pnpm install --prod"),
            "pm_prune_prod": "pnpm prune --prod",
        }

    if normalized == "yarn":
        # Yarn 1.x nao possui "prune"; reinstalar sem devDependencies e o equivalente.
        return {
            "pm_needs_corepack": True,
            "pm_corepack_spec": _resolve_corepack_spec("yarn", package_manager),
            "pm_copy_manifests": "package.json yarn.lock*",
            "pm_install_all": "yarn install --frozen-lockfile" if has_lockfile else "yarn install",
            "pm_install_prod": ("yarn install --frozen-lockfile --production" if has_lockfile else "yarn install --production"),
            "pm_prune_prod": ("yarn install --frozen-lockfile --production" if has_lockfile else "yarn install --production"),
        }

    return {
        "pm_needs_corepack": False,
        "pm_corepack_spec": "",
        "pm_copy_manifests": "package*.json",
        "pm_install_all": "npm ci" if has_lockfile else "npm install",
        "pm_install_prod": ("npm ci --omit=dev" if has_lockfile else "npm install --omit=dev"),
        "pm_prune_prod": "npm prune --omit=dev",
    }


def _resolve_template_name(language: str, has_build_step: bool) -> str:
    """Resolve template de Dockerfile com base na linguagem e build step."""

    normalized_language = language.strip().lower()
    if normalized_language == "node":
        return "node/dockerfile_build.j2" if has_build_step else "node/dockerfile_simple.j2"
    if normalized_language == "python":
        return "python/dockerfile.j2"
    if normalized_language == "java":
        return "java/dockerfile.j2"
    if normalized_language == "go":
        return "go/dockerfile.j2"
    if normalized_language == "dotnet":
        return "dotnet/dockerfile.j2"
    raise ValueError(f"Linguagem sem template de Dockerfile: {language}")


def generate(context: dict[str, Any]) -> str:
    """Renderiza Dockerfile de acordo com linguagem e estratégia de build.

    Args:
        context: Dicionário com dados de renderização.

    Returns:
        Conteúdo do Dockerfile renderizado.
    """

    language = str(context.get("language", "")).strip().lower()
    has_build_step = bool(context.get("has_build_step", False))
    template_name = _resolve_template_name(language, has_build_step)

    render_context = dict(context)
    if language == "node":
        # Injetado aqui para que os templates recebam os comandos prontos
        # mesmo quando o chamador nao informa build_tool/has_lockfile.
        render_context.update(
            node_package_manager_context(
                str(context.get("build_tool", "npm")),
                bool(context.get("has_lockfile", False)),
                str(context.get("package_manager", "")),
            )
        )

    environment = _build_environment()
    template = environment.get_template(template_name)
    return template.render(**render_context).strip() + "\n"
