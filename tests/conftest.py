"""Configuracao compartilhada dos testes."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT_STR = str(PROJECT_ROOT)
if PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, PROJECT_ROOT_STR)


def _build_zip_in_memory(files: dict[str, str]) -> io.BytesIO:
    """Cria arquivo ZIP em memória a partir de pares caminho/conteúdo."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    buffer.seek(0)
    return buffer


@pytest.fixture
def fastapi_zip_buffer() -> io.BytesIO:
    """Fixture de projeto FastAPI mínimo em ZIP na memória."""

    files = {
        "app/pyproject.toml": (
            "[project]\n"
            "name = 'fastapi-sample'\n"
            "version = '0.1.0'\n"
            "dependencies = ['fastapi>=0.111.0', 'uvicorn>=0.30.0']\n"
        ),
        "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "app/.env.example": "PORT=8000\nAPI_KEY=demo\n",
    }
    return _build_zip_in_memory(files)


@pytest.fixture
def express_zip_buffer() -> io.BytesIO:
    """Fixture de projeto Express mínimo em ZIP na memória."""

    files = {
        "express/package.json": (
            '{'
            '"name":"express-sample",'
            '"version":"1.0.0",'
            '"scripts":{"start":"node server.js"},'
            '"dependencies":{"express":"^4.19.2"}'
            "}"
        ),
        "express/server.js": "const express = require('express');\nconst app = express();\napp.listen(3000);\n",
        "express/.env.example": "PORT=3000\n",
    }
    return _build_zip_in_memory(files)


@pytest.fixture
def spring_boot_zip_buffer() -> io.BytesIO:
    """Fixture de projeto Spring Boot mínimo em ZIP na memória."""

    files = {
        "spring/pom.xml": (
            "<project>"
            "<modelVersion>4.0.0</modelVersion>"
            "<groupId>com.example</groupId>"
            "<artifactId>spring-sample</artifactId>"
            "<version>0.0.1-SNAPSHOT</version>"
            "<dependencies>"
            "<dependency>"
            "<groupId>org.springframework.boot</groupId>"
            "<artifactId>spring-boot-starter-web</artifactId>"
            "</dependency>"
            "</dependencies>"
            "</project>"
        ),
        "spring/src/main/java/com/example/App.java": "class App {}",
        "spring/.env.example": "SPRING_PROFILES_ACTIVE=prod\n",
    }
    return _build_zip_in_memory(files)
