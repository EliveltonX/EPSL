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
    "whitenoise.middleware.WhiteNoiseMiddleware",
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


def _porta_aberta(host: str, port: int, timeout: float = 0.25) -> bool:
    """Verifica rapidamente se uma porta TCP está aberta e aceitando conexões."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _descobrir_postgres_na_rede_docker(
    configured_host: str,
    port: int,
    db_name: str,
    db_user: str,
    db_password: str,
) -> str | None:
    """
    Quando o serviço do Supabase é reiniciado no Coolify, o Docker recria o container
    `supabase-db` com um novo IP dinâmico na sub-rede Docker (ex: `10.0.1.10` -> `10.0.1.14`),
    causando `Connection refused` no IP antigo.
    Esta função varre em paralelo (< 0.3s) as sub-redes `/24` do container e nomes comuns
    do Docker para localizar automaticamente o novo IP do PostgreSQL.
    """
    from concurrent.futures import ThreadPoolExecutor

    prefixos_subrede = set()
    partes_cfg = configured_host.split(".")
    if len(partes_cfg) == 4 and all(p.isdigit() for p in partes_cfg):
        prefixos_subrede.add(".".join(partes_cfg[:3]))

    try:
        import psutil

        for iface_addrs in psutil.net_if_addrs().values():
            for snic in iface_addrs:
                if snic.family == socket.AF_INET and not snic.address.startswith("127."):
                    partes = snic.address.split(".")
                    if len(partes) == 4:
                        prefixos_subrede.add(".".join(partes[:3]))
    except Exception:
        pass

    if not prefixos_subrede:
        prefixos_subrede.add("10.0.1")

    candidatos = [
        "supabase-db",
        "db",
        "postgres",
        "172.17.0.1",
        "10.0.0.1",
    ]
    for prefixo in sorted(prefixos_subrede):
        for ultimo_octeto in range(1, 255):
            ip_cand = f"{prefixo}.{ultimo_octeto}"
            if ip_cand != configured_host:
                candidatos.append(ip_cand)

    def _testar_cand(h: str) -> str | None:
        return h if _porta_aberta(h, port, timeout=0.22) else None

    hosts_com_porta_5432 = []
    with ThreadPoolExecutor(max_workers=64) as executor:
        for res in executor.map(_testar_cand, candidatos):
            if res:
                hosts_com_porta_5432.append(res)

    if not hosts_com_porta_5432:
        return None

    # Valida qual dos hosts encontrados na porta 5432 aceita a autenticação do nosso banco
    try:
        import psycopg

        for h in hosts_com_porta_5432:
            try:
                conn = psycopg.connect(
                    host=h,
                    port=port,
                    dbname=db_name,
                    user=db_user,
                    password=db_password,
                    connect_timeout=2,
                )
                conn.close()
                return h
            except Exception:
                continue
    except Exception:
        pass

    return hosts_com_porta_5432[0]


def _resolver_usuario_dono_postgres(
    host: str,
    port: int,
    db_name: str,
    configured_user: str,
    db_password: str,
) -> str:
    """
    No Supabase self-hosted (Coolify), o superusuário real do cluster PostgreSQL é
    `supabase_admin` (que compartilha a mesma senha `SERVICE_PASSWORD_POSTGRES` do usuário `postgres`).
    Se a tabela `licitacoes` foi criada originalmente por `supabase_admin` e o `DB_USER` estiver
    como `postgres` (ou vice-versa), o comando `migrate` falha com:
    `psycopg.errors.InsufficientPrivilege: must be owner of table licitacoes`.
    Esta função detecta automaticamente o dono (`tableowner`) da tabela `licitacoes` (ou o superusuário
    `supabase_admin`) e ajusta o usuário da conexão / permissões automaticamente.
    """
    try:
        import psycopg

        dono_tabela = None
        with psycopg.connect(
            host=host,
            port=port,
            dbname=db_name,
            user=configured_user,
            password=db_password,
            connect_timeout=2,
            autocommit=True,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = 'licitacoes';"
                )
                row = cur.fetchone()
                if row and row[0]:
                    dono_tabela = str(row[0]).strip()

                if dono_tabela and dono_tabela != configured_user:
                    # Tenta assumir a role dona na própria conexão ou transferir ownership
                    try:
                        cur.execute(f'SET ROLE "{dono_tabela}";')
                        cur.execute(f'ALTER TABLE public.licitacoes OWNER TO "{configured_user}";')
                        cur.execute("RESET ROLE;")
                        return configured_user
                    except Exception:
                        pass

        # Testa conectar diretamente com o dono da tabela ou com `supabase_admin`
        candidatos_user = []
        if dono_tabela and dono_tabela != configured_user:
            candidatos_user.append(dono_tabela)
        if "supabase_admin" not in candidatos_user and configured_user != "supabase_admin":
            candidatos_user.append("supabase_admin")

        for cand_user in candidatos_user:
            try:
                with psycopg.connect(
                    host=host,
                    port=port,
                    dbname=db_name,
                    user=cand_user,
                    password=db_password,
                    connect_timeout=2,
                    autocommit=True,
                ) as admin_conn:
                    with admin_conn.cursor() as acur:
                        acur.execute(
                            f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{configured_user}", "{cand_user}";'
                        )
                        acur.execute(
                            f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{configured_user}", "{cand_user}";'
                        )
                    print(
                        f"[EPSL-AUTO-DISCOVERY] Usando usuario proprietario do banco '{cand_user}' "
                        f"(dono da tabela licitacoes: '{dono_tabela or cand_user}') para evitar erro de permissao em migrations.",
                        file=sys.stderr,
                    )
                    return cand_user
            except Exception:
                continue
    except Exception:
        pass

    return configured_user


if IS_DEPLOY:
    _raw_db_host = os.getenv("DB_HOST", "localhost")
    _clean_db_host = _sanitize_db_host(_raw_db_host)
    _db_port = int(os.getenv("DB_PORT", "5432").strip() or "5432")
    _db_name = os.getenv("DB_NAME", "postgres").strip()
    _db_user = os.getenv("DB_USER", "postgres").strip()
    _db_password = os.getenv("DB_PASSWORD", "").strip()
    DB_CONNECTION_WARNING = ""

    # Ignora verificação de rede durante o build da imagem Docker (`collectstatic`)
    _is_collectstatic = "collectstatic" in sys.argv
    _spaced_host = " ".join(_clean_db_host)

    if not _is_collectstatic and not _porta_aberta(_clean_db_host, _db_port, timeout=1.0):
        print(
            f"\n[EPSL-AUTO-DISCOVERY] O host configurado [{_spaced_host}]:{_db_port} recusou conexao "
            "(provavelmente o container supabase-db trocou de IP ao reiniciar). Buscando novo IP na rede Docker...",
            file=sys.stderr,
        )
        _novo_host = _descobrir_postgres_na_rede_docker(
            configured_host=_clean_db_host,
            port=_db_port,
            db_name=_db_name,
            db_user=_db_user,
            db_password=_db_password,
        )
        if _novo_host:
            _spaced_novo = " ".join(_novo_host)
            print(
                f"[EPSL-AUTO-DISCOVERY] PostgreSQL localizado automaticamente no novo endereco: [{_spaced_novo}]:{_db_port}!\n",
                file=sys.stderr,
            )
            _clean_db_host = _novo_host

    if not _is_collectstatic:
        _db_user = _resolver_usuario_dono_postgres(
            host=_clean_db_host,
            port=_db_port,
            db_name=_db_name,
            configured_user=_db_user,
            db_password=_db_password,
        )

    try:
        socket.getaddrinfo(_clean_db_host, _db_port)
        DATABASES = {
            "default": {
                "ENGINE": os.getenv(
                    "DB_ENGINE", "django.db.backends.postgresql"
                ).strip(),
                "NAME": _db_name,
                "USER": _db_user,
                "PASSWORD": _db_password,
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

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

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
FORCE_S3_DIRECT_UPLOAD = os.getenv("FORCE_S3_DIRECT_UPLOAD", "False").lower() in (
    "true",
    "1",
    "yes",
)

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
# Google Gemini API & Limites de Taxa por Modelo (Docs_Vault/Arquitetura/ANOTAÇÕES.md)
# ==============================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'").strip()
_configured_gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
if _configured_gemini_model in (
    "",
    "gemini-2.5-flash",
    "models/gemini-2.5-flash",
    "gemini-3.8-flash",
):
    # Prioriza por padrão o modelo com maior limite diário (500 RPD / 15 RPM) e menor latência
    GEMINI_MODEL = "gemini-3.5-flash-lite"
else:
    GEMINI_MODEL = _configured_gemini_model

# Tabela oficial de cotas ativas da conta Google AI Studio (ordenada por maior RPD/RPM e estabilidade)
GEMINI_MODELS_QUOTAS = [
    {"model": "gemini-3.5-flash-lite", "rpm": 15, "tpm": 250_000, "rpd": 500},
    {"model": "gemini-3.1-flash-lite", "rpm": 15, "tpm": 250_000, "rpd": 500},
    {"model": "gemini-3.6-flash", "rpm": 5, "tpm": 250_000, "rpd": 20},
    {"model": "gemini-3-flash-preview", "rpm": 5, "tpm": 250_000, "rpd": 20},
    {"model": "gemini-3.8-flash", "rpm": 5, "tpm": 250_000, "rpd": 20},
]

GEMINI_FALLBACK_MODELS = [item["model"] for item in GEMINI_MODELS_QUOTAS]
if GEMINI_MODEL not in GEMINI_FALLBACK_MODELS:
    GEMINI_FALLBACK_MODELS.insert(0, GEMINI_MODEL)

# ==============================================================================
# Estratégia Ultra-Econômica de RPD/RPM: Super-Batching (250K TPM) + Cron 10 min
# ==============================================================================
# Agrupa as páginas pré-filtradas de múltiplos PDFs em uma única chamada ao Gemini
# aproveitando a janela de 250.000 TPM (~450.000 caracteres ≈ 112.000 tokens seguros).
GEMINI_SUPERBATCH_MAX_CHARS = int(
    os.getenv("GEMINI_SUPERBATCH_MAX_CHARS", "450000")
)
GEMINI_SUPERBATCH_MAX_FILES = int(
    os.getenv("GEMINI_SUPERBATCH_MAX_FILES", "5")
)
# Pausa mínima obrigatória entre duas chamadas consecutivas à API Gemini (mantém RPM <= 3)
GEMINI_MIN_INTERVAL_BETWEEN_CALLS_SEC = float(
    os.getenv("GEMINI_MIN_INTERVAL_BETWEEN_CALLS_SEC", "20.0")
)

# Agendador em background (verifica novos_diarios/ a cada 10 minutos sem pressa)
PIPELINE_CRON_ENABLED = os.getenv("PIPELINE_CRON_ENABLED", "True").lower() in (
    "true",
    "1",
    "yes",
)
PIPELINE_CRON_INTERVAL_MINUTES = int(
    os.getenv("PIPELINE_CRON_INTERVAL_MINUTES", "10")
)
# Quantidade máxima de PDFs enviados à IA por ciclo automático de 10 minutos
# (PDFs duplicados ou sem match no Regex são arquivados imediatamente sem gastar cota)
PIPELINE_MAX_GEMINI_FILES_PER_CRON = int(
    os.getenv("PIPELINE_MAX_GEMINI_FILES_PER_CRON", "3")
)

PIPELINE_TRIGGER_TOKEN = os.getenv("PIPELINE_TRIGGER_TOKEN", "")
