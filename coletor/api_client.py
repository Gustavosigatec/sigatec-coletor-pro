"""
Cliente HTTP que envia as leituras ao Sigatec.

Adaptado para usar o endpoint /api/bc/ingest existente no Brother Counter.
"""
from __future__ import annotations
import json
from typing import List
from datetime import datetime

import requests

from coletor import config
from coletor.snmp_reader import LeituraImpressora
from coletor.utils import get_logger, carregar_config, salvar_config, get_installation_id

log = get_logger()


def _headers_padrao() -> dict:
    """Headers comuns a todas as requisições pro Sigatec."""
    # DEBUG temporario: loga primeiros/ultimos chars da chave + tamanho.
    # Sem isso nao tem como saber se o exe esta enviando placeholder vazio,
    # chave correta, ou chave embaralhada. NAO loga a chave inteira.
    _k = config.INGEST_KEY or ""
    if _k:
        log.info("DEBUG_KEY: len=%d, primeiros=%r, ultimos=%r",
                 len(_k), _k[:6], _k[-6:])
    else:
        log.warning("DEBUG_KEY: INGEST_KEY VAZIA! placeholder nao foi substituido no build.")
    return {
        "X-API-Key": config.INGEST_KEY,
        "X-Installation-ID": get_installation_id(),
        "Content-Type": "application/json",
        "User-Agent": f"{config.APP_NOME}/{config.APP_VERSAO}",
    }


class SigatecAPIError(Exception):
    pass


def _formatar_data_brt() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _leitura_para_registro(leitura: LeituraImpressora, agente: str) -> dict:
    registro = {
        "date": _formatar_data_brt(),
        "serial": leitura.serial,
        "modelo": leitura.modelo,
        "contagem_paginas": str(leitura.contagem_paginas),
        "folder": "AGENT_LOCAL",
        "from": f"ColetorPro-{agente}",
        "subject": f"Coleta automatica - {leitura.modelo or 'Brother'} - {leitura.ip or 'USB'}",
    }
    if leitura.contador_mono is not None:
        registro["contador_mono"] = leitura.contador_mono
    if leitura.contador_color is not None:
        registro["contador_color"] = leitura.contador_color
    if leitura.nivel_toner_preto is not None:
        registro["toner_preto"] = leitura.nivel_toner_preto
    if leitura.nivel_toner_ciano is not None:
        registro["toner_ciano"] = leitura.nivel_toner_ciano
    if leitura.nivel_toner_magenta is not None:
        registro["toner_magenta"] = leitura.nivel_toner_magenta
    if leitura.nivel_toner_amarelo is not None:
        registro["toner_amarelo"] = leitura.nivel_toner_amarelo
    if leitura.ip:
        registro["ip_origem"] = leitura.ip
    if leitura.origem:
        registro["tipo_conexao"] = leitura.origem
    return registro


def enviar_leituras(leituras: List[LeituraImpressora]) -> dict:
    cfg = carregar_config()
    agente = (cfg.get("nome_agente") or "PC").strip()[:128]

    validas = [l for l in leituras if l.valida()]
    if not validas:
        log.warning("Nenhuma leitura valida para enviar (total bruto: %d)", len(leituras))
        return {"ok": True, "novos": 0, "total": 0, "mensagem": "Nada a enviar"}

    registros = [_leitura_para_registro(l, agente) for l in validas]
    payload = {"registros": registros}

    url = config.SIGATEC_URL.rstrip("/") + config.ENDPOINT_COLETOR
    headers = _headers_padrao()

    log.info("Enviando %d registro(s) para %s (agente=%s, install=%s)",
             len(registros), url, agente, headers["X-Installation-ID"][:8])

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    except requests.RequestException as e:
        log.error("Erro de rede ao enviar: %s", e)
        _registrar_envio(cfg, ok=False, status=f"Erro de rede: {e}")
        raise SigatecAPIError(f"Erro de conexao: {e}") from e

    if resp.status_code == 401:
        msg = "API Key invalida ou nao configurada"
        log.error(msg)
        _registrar_envio(cfg, ok=False, status=msg)
        raise SigatecAPIError(msg)

    if resp.status_code == 404:
        msg = "Endpoint /api/bc/ingest nao encontrado no servidor"
        log.error(msg)
        _registrar_envio(cfg, ok=False, status=msg)
        raise SigatecAPIError(msg)

    if resp.status_code >= 400:
        msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        log.error("Falha HTTP: %s", msg)
        _registrar_envio(cfg, ok=False, status=msg)
        raise SigatecAPIError(msg)

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    if not data.get("ok", True):
        msg = data.get("error", "Erro desconhecido do servidor")
        log.error("Servidor respondeu erro: %s", msg)
        _registrar_envio(cfg, ok=False, status=msg)
        raise SigatecAPIError(msg)

    log.info("Envio OK: %s", data)
    novos = data.get("novos", 0)
    total = data.get("total", 0)
    _registrar_envio(cfg, ok=True, status=f"Enviados {len(registros)} - {novos} novos (total no sistema: {total})")
    return data


def _registrar_envio(cfg: dict, ok: bool, status: str) -> None:
    if ok:
        cfg["ultimo_envio_ok"] = datetime.now().isoformat(timespec="seconds")
    cfg["ultimo_envio_status"] = status
    cfg["ultima_coleta"] = datetime.now().isoformat(timespec="seconds")
    salvar_config(cfg)


def testar_conexao() -> tuple[bool, str]:
    url = config.SIGATEC_URL.rstrip("/") + config.ENDPOINT_COLETOR
    headers = _headers_padrao()
    try:
        resp = requests.post(
            url, headers=headers,
            data=json.dumps({"registros": []}),
            timeout=10,
        )
    except requests.RequestException as e:
        return False, f"Erro de rede: {e}"

    if resp.status_code == 200:
        try:
            data = resp.json()
            if data.get("ok"):
                return True, "Conexao OK"
            return False, f"Servidor respondeu: {data.get('error', 'erro desconhecido')}"
        except ValueError:
            return True, "Conexao OK (resposta nao-JSON)"
    if resp.status_code == 401:
        return False, "API Key invalida"
    if resp.status_code == 404:
        return False, "Endpoint /api/bc/ingest nao encontrado"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
