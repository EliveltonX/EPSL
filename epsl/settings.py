"""
Configurações do Django para o projeto EPSL (Automação de Busca em Diários Oficiais).

Suporta dois modos de operação controlados pela variável `APP_ENV`:
1. `APP_ENV=test` (Padrão local):
   - Banco de Dados: MySQL local (padrão) na porta 3306.
   - Armazenamento de PDFs: Pastas locais em `storage_local/novos_diarios/` e
     `storage_local/diarios_processados/{YYYY}/{MM}/`.
2. `APP_ENV=deploy` (Produção no Coolify):
   - Banco de Dados: PostgreSQL (Supabase).
   - Armazenamento de PDFs: Supabase Storage via protocolo S3 (`boto3`).
"""

import os
import socket
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# ==============================================================================
# Detecção de Ambiente: "test" (local) vs "deploy" (produção Coolify)
# ==============================================================================
APP_ENV = os.getenv("APP_ENV", "test").strip().lower()
IS_DEPLOY = APP_ENV in ("deploy", "production", "prod")

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "django-insecure-epsl-dev-key-altere-em-producao-no-coolify",
)

DEBUG = os.getenv("DEBUG", "False" if IS_DEPLOY else "True").lower() in (
    "true",
    "1",
    "yes",
)

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "ALLOWED_HOSTS",
        "localhost,127.0.0.1,.eliveltonpardini.com.br",
    ).split(",")
    if host.strip()
]

# Configuração automática de CSRF_TRUSTED_ORIGINS e Proxy Reverso (Coolify Traefik / Cloudflare)
_trusted_origins = [
    "https://*.eliveltonpardini.com.br",
    "http://*.eliveltonpardini.com.br",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
for _h in ALLOWED_HOSTS:
    _clean_h = _h.lstrip(".")
    if _clean_h and _clean_h != "*":
        _trusted_origins.append(f"https://{_clean_h}")
        _trusted_origins.append(f"http://{_clean_h}")
        if "." in _clean_h:
            _trusted_origins.append(f"https://*.{_clean_h}")

for _extra_origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(","):
    if _extra_origin.strip():
        _trusted_origins.append(_extra_origin.strip())

CSRF_TRUSTED_ORIGINS = sorted(set(_trusted_origins))
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Apps do projeto EPSL
    "licitacoes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "epsl.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "epsl.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "epsl.wsgi.application"
ASGI_APPLICATION = "epsl.asgi.application"

# ==============================================================================
# Banco de Dados:
# - Em DEPLOY (Coolify): PostgreSQL do Supabase
# - Em TESTE (Local): Padrão local do Django (SQLite3 em arquivo `db.sqlite3`),
#   ou MySQL local caso `TEST_DB_ENGINE=mysql` seja definido no `.env`.
# ==============================================================================
def _sanitize_db_host(raw_host: str) -> str:
    """Remove acidentais prefixos http(s)://, barras ou portas do DB_HOST."""
    host = raw_host.strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
    host = host.split("/")[0].strip()
    if ":" in host and not host.startswith("["):
        host = host.split(":")[0].strip()
    return host


if IS_DEPLOY:
    _raw_db_host = os.getenv("DB_HOST", "localhost")
    _clean_db_host = _sanitize_db_host(_raw_db_host)
    _db_port = int(os.getenv("DB_PORT", "5432").strip() or "5432")
    DB_CONNECTION_WARNING = ""

    # Diagnóstico de DNS no log (espaçado para não ser mascarado como <REDACTED> pelo Coolify)
    _spaced_host = " ".join(_clean_db_host)
    try:
        socket.getaddrinfo(_clean_db_host, _db_port)
        DATABASES = {
            "default": {
                "ENGINE": os.getenv(
                    "DB_ENGINE", "django.db.backends.postgresql"
                ).strip(),
                "NAME": os.getenv("DB_NAME", "postgres").strip(),
                "USER": os.getenv("DB_USER", "postgres").strip(),
                "PASSWORD": os.getenv("DB_PASSWORD", "").strip(),
                "HOST": _clean_db_host,
                "PORT": str(_db_port),
            }
        }
    except socket.gaierror as exc:
        DB_CONNECTION_WARNING = (
            f"Não foi possível resolver o DNS do DB_HOST='{_clean_db_host}' ({exc}). "
            "Verifique se o valor de DB_HOST está correto e se o container do Supabase e do Django "
            "estão conectados na mesma rede Docker ('coolify'). Usando SQLite temporário para não derrubar o container."
        )
        print(
            f"\n[EPSL-DIAGNOSTICO] Falha ao resolver DB_HOST (soletrado): [{_spaced_host}] -> {exc}",
            file=sys.stderr,
        )
        print(
            "[EPSL-DIAGNOSTICO] Usando SQLite temporario para concluir o boot do container no Coolify.\n",
            file=sys.stderr,
        )
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }
else:
    DB_CONNECTION_WARNING = ""
    TEST_DB_ENGINE = os.getenv(
        "TEST_DB_ENGINE", "django.db.backends.sqlite3"
    ).strip()
    if "mysql" in TEST_DB_ENGINE:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.mysql",
                "NAME": os.getenv("MYSQL_DB_NAME", "epsl_test"),
                "USER": os.getenv("MYSQL_DB_USER", "root"),
                "PASSWORD": os.getenv("MYSQL_DB_PASSWORD", ""),
                "HOST": os.getenv("MYSQL_DB_HOST", "127.0.0.1"),
                "PORT": os.getenv("MYSQL_DB_PORT", "3306"),
                "OPTIONS": {
                    "charset": "utf8mb4",
                },
            }
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / os.getenv("SQLITE_DB_NAME", "db.sqlite3"),
            }
        }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

LOGIN_URL = "/"
LOGIN_REDIRECT_URL = "/licitacoes/"
LOGOUT_REDIRECT_URL = "/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ==============================================================================
# Armazenamento de Arquivos (Storage):
# - Em TESTE (`USE_LOCAL_STORAGE = True`): Pastas locais em `storage_local/`
# - Em DEPLOY (`USE_LOCAL_STORAGE = False`): Supabase Storage via S3 (`boto3`)
# ==============================================================================
USE_LOCAL_STORAGE = not IS_DEPLOY

LOCAL_STORAGE_ROOT = Path(
    os.getenv("LOCAL_STORAGE_ROOT", str(BASE_DIR / "storage_local"))
)
MEDIA_URL = "/media/"
MEDIA_ROOT = LOCAL_STORAGE_ROOT
LOCAL_MEDIA_BASE_URL = os.getenv(
    "LOCAL_MEDIA_BASE_URL", "http://localhost:8000/media"
).rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "http://localhost:8000")
SUPABASE_PUBLIC_STORAGE_URL = os.getenv(
    "SUPABASE_PUBLIC_STORAGE_URL",
    f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public",
)
SUPABASE_S3_ENDPOINT_URL = os.getenv(
    "SUPABASE_S3_ENDPOINT_URL",
    f"{SUPABASE_URL.rstrip('/')}/storage/v1/s3",
)
SUPABASE_S3_REGION_NAME = os.getenv("SUPABASE_S3_REGION_NAME", "us-east-1")
SUPABASE_S3_ACCESS_KEY_ID = os.getenv("SUPABASE_S3_ACCESS_KEY_ID", "")
SUPABASE_S3_SECRET_ACCESS_KEY = os.getenv("SUPABASE_S3_SECRET_ACCESS_KEY", "")

SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "diarios-oficiais")
SUPABASE_INPUT_PREFIX = os.getenv("SUPABASE_INPUT_PREFIX", "novos_diarios/")
SUPABASE_PROCESSED_PREFIX = os.getenv("SUPABASE_PROCESSED_PREFIX", "diarios_processados/")

# ==============================================================================
# Google Gemini API & Segurança do Webhook
# ==============================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

PIPELINE_TRIGGER_TOKEN = os.getenv("PIPELINE_TRIGGER_TOKEN", "")
