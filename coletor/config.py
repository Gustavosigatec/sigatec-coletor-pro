"""
Configuração do Sigatec Coletor Pro.

Valores embutidos no executável no momento do build (PyInstaller).
Em desenvolvimento, podem ser sobrescritos por variáveis de ambiente
(arquivo .env na raiz do projeto).

SEGURANÇA:
 - A INGEST_KEY é escopada no servidor: só tem permissão de POSTar em
   /api/bc/ingest. Não abre admin, não lê, não apaga. Se vazar, o impacto
   é limitado a spam de leituras.
 - No .exe de produção, a chave é armazenada XOR-ofuscada para evitar que
   um `strings` trivial revele a chave. Isso NÃO é criptografia — um
   reverser determinado consegue extrair. A mitigação real de segurança é:
     (a) chave escopada no servidor
     (b) rate limiting no servidor
     (c) chave única por cliente
     (d) ofuscação aqui como speed bump

NUNCA commitar este arquivo com INGEST_KEY ofuscada real preenchida.
Use .env (no .gitignore) em desenvolvimento.
"""

import base64
import os
from pathlib import Path

# Carrega .env em desenvolvimento (opcional — se python-dotenv estiver instalado)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass


# ═══ Ofuscação XOR da chave embutida ═════════════════════════════════════════
_XOR_CONST = b"Sig4t3cColetorPro_xor_key_do_not_change_v1"


def _xor_decode(encoded_b64: str) -> str:
    """Decodifica uma string XOR-ofuscada + base64 → chave plaintext."""
    if not encoded_b64 or encoded_b64 == "__PLACEHOLDER_ENCODED_KEY__":
        return ""
    try:
        xored = base64.b64decode(encoded_b64)
        plain = bytes(b ^ _XOR_CONST[i % len(_XOR_CONST)] for i, b in enumerate(xored))
        return plain.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _xor_encode(plaintext: str) -> str:
    """Codifica uma chave plaintext → string XOR-ofuscada + base64.
    Usado pelo installer/build.py em tempo de build.
    """
    data = plaintext.encode("utf-8")
    xored = bytes(b ^ _XOR_CONST[i % len(_XOR_CONST)] for i, b in enumerate(data))
    return base64.b64encode(xored).decode("ascii")


# ═══ Conexão com o Sigatec ═══════════════════════════════════════════════════

# URL base do sistema Sigatec (sem barra no final)
SIGATEC_URL = os.getenv(
    "SIGATEC_URL",
    "https://sigatec-sistema-production.up.railway.app"
)

# Endpoint que recebe as leituras
ENDPOINT_COLETOR = "/api/bc/ingest"

# ─── Chave de ingest (escopo limitado no servidor) ───────────────────────────
_INGEST_KEY_OFUSCADA = "__PLACEHOLDER_ENCODED_KEY__"

INGEST_KEY = (
    os.getenv("SIGATEC_INGEST_KEY")
    or os.getenv("SIGATEC_API_KEY")
    or _xor_decode(_INGEST_KEY_OFUSCADA)
)

# Alias retro-compatível
API_KEY = INGEST_KEY


# ═══ Rede / SNMP ══════════════════════════════════════════════════════════════

RANGE_REDE = os.getenv("COLETOR_RANGE_REDE") or None
SNMP_TIMEOUT = int(os.getenv("COLETOR_SNMP_TIMEOUT", "2"))
SNMP_RETRIES = int(os.getenv("COLETOR_SNMP_RETRIES", "1"))
SNMP_COMMUNITY = os.getenv("COLETOR_SNMP_COMMUNITY", "public")
SCAN_WORKERS = 64


# ═══ Agendamento ══════════════════════════════════════════════════════════════

HORARIO_ENVIO_PADRAO = "18:00"


# ═══ Arquivos locais ══════════════════════════════════════════════════════════

# Pasta em %APPDATA%\SigatecColetorPro (separada do coletor original)
NOME_PASTA_APPDATA = "SigatecColetorPro"
ARQUIVO_CONFIG = "config.json"
ARQUIVO_LOG = "coletor.log"
ARQUIVO_INSTALLATION_ID = "installation_id.txt"


# ═══ Tunnel WebSocket ══════════════════════════════════════════════════════════

# URL do endpoint WebSocket do servidor central
TUNNEL_WS_URL = os.getenv(
    "TUNNEL_WS_URL",
    "wss://sigatec-sistema-production.up.railway.app/api/bc/tunnel/ws"
)

# Liga/desliga o módulo de acesso remoto
TUNNEL_ATIVO = os.getenv("TUNNEL_ATIVO", "true").lower() not in ("false", "0", "no")

# Backoff: espera mínima e máxima (segundos) entre tentativas de reconexão
TUNNEL_BACKOFF_MIN = 2
TUNNEL_BACKOFF_MAX = 60

# Intervalo de heartbeat (segundos)
TUNNEL_HEARTBEAT_INTERVAL = 30


# ═══ Identificação ════════════════════════════════════════════════════════════

APP_NOME = "Sigatec Coletor Pro"
APP_VERSAO = "1.0.0"
APP_AUTOR = "Sigatec"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_RUN_NAME = "SigatecColetorPro"
