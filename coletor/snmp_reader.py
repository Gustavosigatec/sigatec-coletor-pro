"""
Leitura de impressoras Brother via SNMP.

Usa puresnmp 1.x (API síncrona, sem plugin system).
Versão 2.x do puresnmp tem plugin discovery via entry_points que quebra em .exe
congelado pelo PyInstaller — retornava "UnknownMessageProcessingModel: Known
identifiers: []". Por isso fixamos em 1.11.0.
"""
import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional, List

from puresnmp import get as _puresnmp_get

from coletor import config
from coletor.utils import get_logger

log = get_logger()

# Captura de erros de SNMP durante a varredura (pra diagnóstico no .exe)
# Thread-safe via GIL (list.append é atômico em CPython)
_ERROS_SNMP_CAPTURADOS: list = []
_ERROS_SNMP_MAX = 5


def _capturar_erro_snmp(ip: str, exc: Exception) -> None:
    """Guarda os primeiros N erros de SNMP de uma varredura pra log."""
    if len(_ERROS_SNMP_CAPTURADOS) < _ERROS_SNMP_MAX:
        try:
            _ERROS_SNMP_CAPTURADOS.append(
                f"{ip}: {type(exc).__name__}: {str(exc)[:180]}"
            )
        except Exception:
            pass


OID_SYS_DESCR         = "1.3.6.1.2.1.1.1.0"
OID_MODELO_BROTHER    = "1.3.6.1.4.1.2435.2.3.9.1.1.7.0"
OID_SERIAL_BROTHER    = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0"
OID_SERIAL_GENERICO   = "1.3.6.1.2.1.43.5.1.1.17.1"
OID_CONTADOR_TOTAL    = "1.3.6.1.2.1.43.10.2.1.4.1.1"
OID_CONTADOR_MONO_BR  = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.0"
OID_CONTADOR_COLOR_BR = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.6.0"
OID_TONER_MAX_K = "1.3.6.1.2.1.43.11.1.1.8.1.1"
OID_TONER_CUR_K = "1.3.6.1.2.1.43.11.1.1.9.1.1"
OID_TONER_MAX_C = "1.3.6.1.2.1.43.11.1.1.8.1.2"
OID_TONER_CUR_C = "1.3.6.1.2.1.43.11.1.1.9.1.2"
OID_TONER_MAX_M = "1.3.6.1.2.1.43.11.1.1.8.1.3"
OID_TONER_CUR_M = "1.3.6.1.2.1.43.11.1.1.9.1.3"
OID_TONER_MAX_Y = "1.3.6.1.2.1.43.11.1.1.8.1.4"
OID_TONER_CUR_Y = "1.3.6.1.2.1.43.11.1.1.9.1.4"


@dataclass
class LeituraImpressora:
    ip: str
    serial: str = ""
    modelo: str = ""
    contagem_paginas: int = 0
    contador_mono:  Optional[int] = None
    contador_color: Optional[int] = None
    nivel_toner_preto:   Optional[int] = None
    nivel_toner_ciano:   Optional[int] = None
    nivel_toner_magenta: Optional[int] = None
    nivel_toner_amarelo: Optional[int] = None
    origem: str = "rede"
    erro: Optional[str] = None

    def valida(self) -> bool:
        return bool(self.serial) and self.contagem_paginas > 0

    def to_payload(self) -> dict:
        d = {"serial": self.serial, "modelo": self.modelo,
             "contagem_paginas": int(self.contagem_paginas),
             "ip": self.ip, "origem": self.origem}
        if self.contador_mono  is not None: d["contador_mono"]  = self.contador_mono
        if self.contador_color is not None: d["contador_color"] = self.contador_color
        if self.nivel_toner_preto   is not None: d["nivel_toner_preto"]   = self.nivel_toner_preto
        if self.nivel_toner_ciano   is not None: d["nivel_toner_ciano"]   = self.nivel_toner_ciano
        if self.nivel_toner_magenta is not None: d["nivel_toner_magenta"] = self.nivel_toner_magenta
        if self.nivel_toner_amarelo is not None: d["nivel_toner_amarelo"] = self.nivel_toner_amarelo
        return d


def _snmp_get(ip, oid, timeout=None, community=None):
    """Chama SNMP GET síncrono via puresnmp 1.x."""
    timeout   = timeout   if timeout   is not None else config.SNMP_TIMEOUT
    community = community if community is not None else config.SNMP_COMMUNITY
    try:
        # puresnmp 1.x API: get(ip, community, oid, port=161, timeout=N)
        result = _puresnmp_get(ip, community, oid, port=161, timeout=timeout)
        if result is None:
            return None
        if isinstance(result, bytes):
            try:
                return result.decode("utf-8", errors="ignore").strip()
            except Exception:
                return str(result)
        return str(result).strip()
    except Exception as e:
        # Captura o erro pra diagnóstico
        _capturar_erro_snmp(ip, e)
        return None


def _snmp_get_int(ip, oid, **kw):
    v = _snmp_get(ip, oid, **kw)
    if v is None: return None
    try: return int(v)
    except (ValueError, TypeError): return None


def _calcular_percentual_toner(ip, oid_max, oid_cur):
    maximo = _snmp_get_int(ip, oid_max)
    atual  = _snmp_get_int(ip, oid_cur)
    if not maximo or maximo <= 0 or atual is None: return None
    if atual < 0: return None
    pct = round((atual / maximo) * 100)
    return max(0, min(100, pct))


def _extrair_modelo(texto):
    """Extrai um nome de modelo limpo da string bruta do SNMP.

    Formatos aceitos:
      'MFG:Brother;CMD:PJL;MDL:DCP-L5512DN;CLS:PRINTER;...' -> 'DCP-L5512DN'
      'Brother HL-L2350DW series'                           -> 'HL-L2350DW'
      'DCP-L5652DN'                                         -> 'DCP-L5652DN'
      ''                                                    -> ''
    """
    if not texto:
        return ""
    t = texto.strip()

    # Formato IEEE 1284 (MFG:...;MDL:...;)
    m = re.search(r"MDL\s*:\s*([^;]+?)\s*(?:;|$)", t, re.IGNORECASE)
    if m:
        modelo = m.group(1).strip()
        modelo = re.sub(r"^Brother\s+", "", modelo, flags=re.IGNORECASE).strip()
        return modelo[:128]

    # Formato "Brother XXXX series" ou "Brother XXXX"
    m = re.match(r"^\s*Brother\s+(\S+(?:[-/]\S+)*)", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:128]

    # Ja parece modelo limpo (DCP-L5652DN, HL-L2350DW, MFC-8512DN)
    if re.match(r"^[A-Z]{2,4}-\w+$", t):
        return t[:128]

    return t[:128]


def eh_brother(ip):
    descr = _snmp_get(ip, OID_SYS_DESCR)
    if not descr: return False
    return "BROTHER" in descr.upper()


def ler_impressora(ip, origem="rede"):
    leitura = LeituraImpressora(ip=ip, origem=origem)

    modelo_raw = _snmp_get(ip, OID_MODELO_BROTHER)
    if not modelo_raw:
        modelo_raw = _snmp_get(ip, OID_SYS_DESCR) or ""
    leitura.modelo = _extrair_modelo(modelo_raw)

    serial = _snmp_get(ip, OID_SERIAL_BROTHER) or _snmp_get(ip, OID_SERIAL_GENERICO)
    leitura.serial = (serial or "").strip().upper()[:64]

    total = _snmp_get_int(ip, OID_CONTADOR_TOTAL)
    if total is not None: leitura.contagem_paginas = total
    mono = _snmp_get_int(ip, OID_CONTADOR_MONO_BR)
    if mono is not None: leitura.contador_mono = mono
    color = _snmp_get_int(ip, OID_CONTADOR_COLOR_BR)
    if color is not None: leitura.contador_color = color

    leitura.nivel_toner_preto   = _calcular_percentual_toner(ip, OID_TONER_MAX_K, OID_TONER_CUR_K)
    leitura.nivel_toner_ciano   = _calcular_percentual_toner(ip, OID_TONER_MAX_C, OID_TONER_CUR_C)
    leitura.nivel_toner_magenta = _calcular_percentual_toner(ip, OID_TONER_MAX_M, OID_TONER_CUR_M)
    leitura.nivel_toner_amarelo = _calcular_percentual_toner(ip, OID_TONER_MAX_Y, OID_TONER_CUR_Y)

    if not leitura.serial:
        leitura.erro = "Nao foi possivel ler o numero de serie"
    elif leitura.contagem_paginas == 0 and not leitura.contador_mono:
        leitura.erro = "Nao foi possivel ler contador de paginas"

    return leitura


def detectar_rede_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_local = s.getsockname()[0]
        s.close()
        partes = ip_local.split(".")
        return f"{partes[0]}.{partes[1]}.{partes[2]}.0/24"
    except Exception as e:
        log.warning("Falha detectando rede local: %s", e)
        return None


def varrer_rede(rede_cidr=None, callback_progresso=None):
    rede_cidr = rede_cidr or config.RANGE_REDE or detectar_rede_local()
    if not rede_cidr:
        log.error("Nao foi possivel determinar a rede para varrer")
        return []
    log.info("Iniciando varredura em %s", rede_cidr)
    try:
        rede = ipaddress.ip_network(rede_cidr, strict=False)
    except ValueError as e:
        log.error("CIDR invalido %s: %s", rede_cidr, e)
        return []
    ips_candidatos = [str(ip) for ip in rede.hosts()]
    total = len(ips_candidatos)
    brothers = []
    inicio = time.monotonic()
    # Limpa captura de erros do scan anterior
    _ERROS_SNMP_CAPTURADOS.clear()
    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as pool:
        futuros = {pool.submit(eh_brother, ip): ip for ip in ips_candidatos}
        concluidos = 0
        for fut in as_completed(futuros):
            ip = futuros[fut]
            concluidos += 1
            try:
                if fut.result():
                    brothers.append(ip)
                    log.info("Brother encontrada: %s", ip)
            except Exception as e:
                log.debug("Erro SNMP (worker) %s: %s", ip, e)
            if callback_progresso:
                try: callback_progresso(concluidos, total, ip)
                except Exception: pass
    duracao = time.monotonic() - inicio
    log.info("Varredura concluida: %d Brother(s) em %d IPs (%.1fs, %.2fms/IP)",
             len(brothers), total, duracao, (duracao * 1000) / max(1, total))
    if duracao < 3 and len(brothers) == 0:
        erros_str = (
            "; ".join(_ERROS_SNMP_CAPTURADOS) if _ERROS_SNMP_CAPTURADOS
            else "(nenhum capturado — asyncio/puresnmp com problema estrutural no .exe?)"
        )
        log.warning("Varredura finalizou rápido demais (%.1fs) — suspeita de "
                    "firewall bloqueando UDP 161 OU biblioteca SNMP com erro. "
                    "Erros capturados: %s", duracao, erros_str)
    return sorted(brothers, key=lambda x: tuple(int(p) for p in x.split(".")))


def coletar_de_ips(ips, callback_progresso=None):
    resultados = []
    total = len(ips)
    with ThreadPoolExecutor(max_workers=min(16, max(1, total))) as pool:
        futuros = {pool.submit(ler_impressora, ip, "rede"): ip for ip in ips}
        concluidos = 0
        for fut in as_completed(futuros):
            ip = futuros[fut]
            concluidos += 1
            try:
                leitura = fut.result()
            except Exception as e:
                log.error("Erro lendo %s: %s", ip, e)
                leitura = LeituraImpressora(ip=ip, erro=str(e))
            resultados.append(leitura)
            if callback_progresso:
                try: callback_progresso(concluidos, total, ip)
                except Exception: pass
    return resultados
