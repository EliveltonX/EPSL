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
# Necessário para que servidores de tiles do OpenStreetMap recebam o header Referer
# (o padrão "same-origin" do Django omite o Referer e causa bloqueio http://osm.wiki/Blocked)
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Apps do projeto EPSL
    "licitacoes",
    "credenciamento_parana",
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


def _validar_e_preparar_postgres(
    host: str,
    port: int,
    db_name: str,
    configured_user: str,
    db_password: str,
    timeout: float = 2.0,
) -> tuple[bool, str, str, bool, str]:
    """
    Testa se `host:port` está acessível E aceita autenticação PostgreSQL com as credenciais
    do EPSL, diferenciando o container real `supabase-db` de outros containers PostgreSQL
    que possam ter assumido o IP antigo na rede Docker do Coolify.

    Retorna:
      (autenticou_ok, usuario_resolvido, senha_resolvida, possui_assinatura_epsl, motivo)
    """
    if not _porta_aberta(host, port, timeout=min(timeout, 0.8)):
        return (False, configured_user, db_password, False, "porta TCP fechada (Connection refused)")

    try:
        import psycopg
    except Exception as exc:
        return (True, configured_user, db_password, False, f"psycopg indisponivel ({exc})")

    usuarios_cand = []
    for u in (configured_user, "supabase_admin", "postgres"):
        u_clean = (u or "").strip()
        if u_clean and u_clean not in usuarios_cand:
            usuarios_cand.append(u_clean)

    senhas_cand = []
    for s in (
        db_password,
        db_password.strip("\"'"),
        os.getenv("SERVICE_PASSWORD_POSTGRES", "").strip(),
        os.getenv("POSTGRES_PASSWORD", "").strip(),
    ):
        if s and s not in senhas_cand:
            senhas_cand.append(s)
    if not senhas_cand:
        senhas_cand = [""]

    ultimo_erro = "falha de autenticacao"
    for senha_teste in senhas_cand:
        for user_teste in usuarios_cand:
            try:
                with psycopg.connect(
                    host=host,
                    port=port,
                    dbname=db_name,
                    user=user_teste,
                    password=senha_teste,
                    connect_timeout=max(1, int(timeout)),
                    autocommit=True,
                ) as conn:
                    with conn.cursor() as cur:
                        # Verifica se este Postgres já possui tabelas do EPSL ou roles do Supabase
                        cur.execute(
                            """
                            SELECT
                                EXISTS (
                                    SELECT 1 FROM pg_tables
                                    WHERE schemaname = 'public'
                                      AND tablename IN ('licitacoes', 'django_migrations', 'credenciamento_escolas')
                                ) AS tem_tabelas,
                                EXISTS (
                                    SELECT 1 FROM pg_roles WHERE rolname = 'supabase_admin'
                                ) AS eh_supabase,
                                (
                                    SELECT tableowner FROM pg_tables
                                    WHERE schemaname = 'public' AND tablename = 'licitacoes'
                                    LIMIT 1
                                ) AS dono_licitacoes;
                            """
                        )
                        row = cur.fetchone()
                        tem_tabelas = bool(row and row[0])
                        eh_supabase = bool(row and row[1])
                        dono_tabela = str(row[2]).strip() if (row and row[2]) else None

                        usuario_final = user_teste

                        # Se a tabela `licitacoes` pertence a outra role (ex: `supabase_admin`),
                        # garante permissões e usa a role dona para evitar erro `must be owner of table`
                        if dono_tabela and dono_tabela != user_teste:
                            try:
                                cur.execute(f'SET ROLE "{dono_tabela}";')
                                cur.execute(f'ALTER TABLE public.licitacoes OWNER TO "{user_teste}";')
                                cur.execute("RESET ROLE;")
                            except Exception:
                                try:
                                    with psycopg.connect(
                                        host=host,
                                        port=port,
                                        dbname=db_name,
                                        user=dono_tabela,
                                        password=senha_teste,
                                        connect_timeout=2,
                                        autocommit=True,
                                    ) as owner_conn:
                                        with owner_conn.cursor() as ocur:
                                            ocur.execute(
                                                f'GRANT ALL PRIVILEGES ON SCHEMA public TO "{configured_user}", "{dono_tabela}";'
                                            )
                                            ocur.execute(
                                                f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{configured_user}", "{dono_tabela}";'
                                            )
                                            ocur.execute(
                                                f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{configured_user}", "{dono_tabela}";'
                                            )
                                        usuario_final = dono_tabela
                                except Exception:
                                    pass

                        return (
                            True,
                            usuario_final,
                            senha_teste,
                            (tem_tabelas or eh_supabase),
                            f"autenticado (user={usuario_final}, tabelas_epsl={tem_tabelas}, supabase={eh_supabase})",
                        )
            except Exception as exc:
                ultimo_erro = str(exc).splitlines()[0].strip()
                continue

    return (False, configured_user, db_password, False, ultimo_erro)


def _descobrir_postgres_na_rede_docker(
    configured_host: str,
    port: int,
    db_name: str,
    db_user: str,
    db_password: str,
) -> tuple[str, str, str] | None:
    """
    Quando o serviço do Supabase ou do Coolify é reiniciado, o Docker pode:
    1. Recriar o container `supabase-db` em outro IP dinâmico (ex: `10.0.1.10` -> `10.0.1.5`);
    2. Atribuir o IP antigo (`10.0.1.10`) a OUTRO container PostgreSQL da mesma rede Docker,
       o que faria um simples teste de porta TCP (`5432`) passar, mas falhar com
       `FATAL: password authentication failed for user "postgres"`.

    Esta função varre em paralelo (< 0.4s) as sub-redes Docker e nomes de containers,
    testa a autenticação real (`_validar_e_preparar_postgres`) em todos os IPs com porta 5432
    aberta e retorna `(host_correto, user_correto, password_correta)` do nosso `supabase-db`.
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

    # Garante sempre as sub-redes padrão do Coolify / Docker
    prefixos_subrede.update(["10.0.1", "10.0.0"])

    candidatos = [
        "supabase-db",
        "db",
        "postgres",
        "172.17.0.1",
        "10.0.0.1",
    ]
    vistos = set(candidatos)
    for prefixo in sorted(prefixos_subrede):
        for ultimo_octeto in range(1, 255):
            ip_cand = f"{prefixo}.{ultimo_octeto}"
            if ip_cand != configured_host and ip_cand not in vistos:
                candidatos.append(ip_cand)
                vistos.add(ip_cand)

    def _testar_cand(h: str) -> str | None:
        return h if _porta_aberta(h, port, timeout=0.25) else None

    hosts_com_porta_5432 = []
    with ThreadPoolExecutor(max_workers=64) as executor:
        for res in executor.map(_testar_cand, candidatos):
            if res:
                hosts_com_porta_5432.append(res)

    if not hosts_com_porta_5432:
        return None

    autenticados_com_assinatura = []
    autenticados_genericos = []

    for h in hosts_com_porta_5432:
        ok, user_ok, pass_ok, tem_assinatura, motivo = _validar_e_preparar_postgres(
            host=h,
            port=port,
            db_name=db_name,
            configured_user=db_user,
            db_password=db_password,
            timeout=2.0,
        )
        _spaced_h = " ".join(h)
        if ok:
            print(
                f"[EPSL-AUTO-DISCOVERY] Candidato [{_spaced_h}]:{port} aceitou autenticacao ({motivo}).",
                file=sys.stderr,
            )
            if tem_assinatura:
                autenticados_com_assinatura.append((h, user_ok, pass_ok))
            else:
                autenticados_genericos.append((h, user_ok, pass_ok))
        else:
            print(
                f"[EPSL-AUTO-DISCOVERY] Ignorando [{_spaced_h}]:{port} -> pertence a outro container Postgres ({motivo}).",
                file=sys.stderr,
            )

    if autenticados_com_assinatura:
        return autenticados_com_assinatura[0]
    if autenticados_genericos:
        return autenticados_genericos[0]

    return None


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

    if not _is_collectstatic:
        _host_ok, _resolved_user, _resolved_pass, _, _motivo_host = _validar_e_preparar_postgres(
            host=_clean_db_host,
            port=_db_port,
            db_name=_db_name,
            configured_user=_db_user,
            db_password=_db_password,
            timeout=1.5,
        )
        if _host_ok:
            _db_user = _resolved_user
            _db_password = _resolved_pass
        else:
            print(
                f"\n[EPSL-AUTO-DISCOVERY] O host configurado [{_spaced_host}]:{_db_port} falhou na verificacao "
                f"({_motivo_host}). Provavelmente o container supabase-db trocou de IP na rede Docker e outro "
                "container assumiu o IP antigo. Buscando o container supabase-db correto...",
                file=sys.stderr,
            )
            _descoberto = _descobrir_postgres_na_rede_docker(
                configured_host=_clean_db_host,
                port=_db_port,
                db_name=_db_name,
                db_user=_db_user,
                db_password=_db_password,
            )
            if _descoberto:
                _novo_host, _novo_user, _nova_senha = _descoberto
                _spaced_novo = " ".join(_novo_host)
                print(
                    f"[EPSL-AUTO-DISCOVERY] PostgreSQL do EPSL localizado e autenticado automaticamente em: "
                    f"[{_spaced_novo}]:{_db_port} (user='{_novo_user}')!\n",
                    file=sys.stderr,
                )
                _clean_db_host = _novo_host
                _db_user = _novo_user
                _db_password = _nova_senha
            else:
                print(
                    f"[EPSL-AUTO-DISCOVERY] ALERTA: Nenhum container PostgreSQL na rede Docker aceitou a senha "
                    f"configurada em DB_PASSWORD para o banco '{_db_name}'. Verifique se o container supabase-db "
                    "esta rodando e conectado na rede 'coolify' ou se DB_PASSWORD corresponde a SERVICE_PASSWORD_POSTGRES.\n",
                    file=sys.stderr,
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
