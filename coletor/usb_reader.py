"""
Detecção e leitura de impressoras Brother conectadas por USB no Windows.

Métodos de leitura (tentados em sequência — o primeiro que der certo ganha):

 1) **USB bidirecional direto** (estilo BRAdmin): enumera dispositivos USBPRINT
    via SetupAPI, abre com CreateFile e troca PJL pelo canal (bypass spooler).
    Funciona com Brother laser E jato de tinta. Ver `coletor/usb_bidi.py`.

 2) **SNMP em 127.0.0.1**: algumas Brother laser instalam Status Monitor
    que expõe proxy SNMP local. Não funciona em jato (DCP-T, MFC-J).

 3) **WMI/PowerShell Get-Printer**: só serve pra descobrir quais impressoras
    Brother estão instaladas; a leitura dos contadores em si vem dos métodos
    acima.

Em Linux/macOS, este módulo retorna lista vazia (USB é só Windows).
"""

import os
import subprocess
from typing import List, Optional

from coletor.snmp_reader import LeituraImpressora, ler_impressora
from coletor.utils import get_logger

log = get_logger()


# Prefixos de porta local aceitos pelo filtro WMI (pra UI/diagnóstico)
PREFIXOS_PORTA_LOCAL = ("USB", "DOT4", "BRUSB", "BRN", "WSD-", "WSDPRINT")


# ═══ WMI (PowerShell Get-Printer) ═════════════════════════════════════════════

def listar_impressoras_wmi() -> List[dict]:
    """Retorna [{nome, portname, driver}] das impressoras instaladas no Windows."""
    if os.name != "nt":
        return []

    resultado: List[dict] = []
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            "Get-Printer | Select-Object Name,PortName,DriverName | ConvertTo-Json -Compress"
        ]
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if out.returncode != 0:
            log.warning("Get-Printer falhou: %s", out.stderr.strip())
            return []

        import json
        data = json.loads(out.stdout or "[]")
        if isinstance(data, dict):
            data = [data]

        for item in data:
            nome = (item.get("Name") or "").strip()
            porta = (item.get("PortName") or "").strip()
            driver = (item.get("DriverName") or "").strip()
            resultado.append({"nome": nome, "portname": porta, "driver": driver})
    except Exception as e:
        log.warning("Erro listando impressoras WMI: %s", e)

    return resultado


def filtrar_brother_usb(lista: List[dict]) -> List[dict]:
    """Filtra só as Brother conectadas por porta local (USB/Dot4/WSD/BRN/etc.)."""
    brothers = []
    for p in lista:
        driver = (p.get("driver") or "").upper()
        nome = (p.get("nome") or "").upper()
        porta = (p.get("portname") or "").upper()

        eh_brother = "BROTHER" in driver or "BROTHER" in nome
        eh_local = porta.startswith(PREFIXOS_PORTA_LOCAL)

        if eh_brother and eh_local:
            brothers.append(p)
    return brothers


# ═══ Método 1 (primário): USB bidirecional direto via SetupAPI ═══════════════

def ler_via_usb_direto() -> List[LeituraImpressora]:
    """Enumera USBPRINT e lê cada Brother USB direto pelo canal bidirecional.

    É o mesmo caminho que BRAdmin usa. Funciona pra laser e jato.
    """
    if os.name != "nt":
        return []

    try:
        from coletor import usb_bidi
    except ImportError as e:
        log.warning("Não foi possível importar usb_bidi: %s", e)
        return []

    resultados: List[LeituraImpressora] = []

    dispositivos = usb_bidi.enumerar_dispositivos_usbprint()
    brothers = [d for d in dispositivos if d.get("brother")]
    log.info("SetupAPI: %d dispositivo(s) USBPRINT, %d Brother",
             len(dispositivos), len(brothers))

    for disp in brothers:
        inst_id = disp.get("instance_id") or "?"
        dev_path = disp.get("device_path")
        if not dev_path:
            continue

        log.debug("USB direto: abrindo %s", inst_id)
        try:
            resposta = usb_bidi.enviar_e_ler_pjl(
                dev_path, usb_bidi.COMANDO_PJL_INFO
            )
        except Exception as e:
            log.warning("USB direto: exceção em %s: %s", inst_id, e)
            continue

        if not resposta:
            log.warning("USB direto: sem resposta PJL de %s", inst_id)
            continue

        dados = usb_bidi.parsear_resposta_pjl(resposta)
        info_inst = usb_bidi.info_da_instance_id(inst_id)

        # Se PJL não devolveu serial, tenta pegar do descriptor USB via PowerShell
        serial = (dados.get("serial") or "").upper()
        if not serial:
            serial_usb = usb_bidi.obter_serial_usb_via_pnp(inst_id)
            if serial_usb:
                serial = serial_usb.upper()
                log.info("Serial obtido do USB descriptor (fallback): %s", serial)

        leitura = LeituraImpressora(
            ip="usb",
            serial=serial[:64],
            modelo=(dados.get("modelo") or info_inst.get("modelo_hint") or "").strip()[:128],
            contagem_paginas=int(dados.get("pagecount") or 0),
            contador_mono=dados.get("pagecount_mono"),
            contador_color=dados.get("pagecount_color"),
            nivel_toner_preto=dados.get("ink_preto"),
            nivel_toner_ciano=dados.get("ink_ciano"),
            nivel_toner_magenta=dados.get("ink_magenta"),
            nivel_toner_amarelo=dados.get("ink_amarelo"),
            origem="usb",
        )

        if leitura.valida():
            resultados.append(leitura)
            log.info("USB direto OK: serial=%s modelo=%s paginas=%d",
                     leitura.serial, leitura.modelo, leitura.contagem_paginas)
        else:
            log.warning(
                "USB direto: resposta recebida (%d bytes) mas não foi possível "
                "extrair serial+contador. Instance=%s. Dados parseados: %r",
                len(resposta), inst_id, dados
            )
            log.debug("Resposta bruta (primeiros 800 bytes): %r", resposta[:800])

    return resultados


# ═══ Método 2: SNMP em 127.0.0.1 (fallback pra laser com Status Monitor) ═════

def tentar_snmp_local() -> Optional[LeituraImpressora]:
    """Tenta ler SNMP em 127.0.0.1 — usado por drivers Brother laser antigos."""
    try:
        leitura = ler_impressora("127.0.0.1", origem="usb")
        if leitura.valida():
            return leitura
    except Exception as e:
        log.debug("SNMP local falhou: %s", e)
    return None


# ═══ API pública ══════════════════════════════════════════════════════════════

def coletar_usb() -> List[LeituraImpressora]:
    """Coleta leituras de impressoras Brother conectadas por USB.

    Ordem de tentativa:
      1) USB direto bidirecional (SetupAPI + CreateFile + PJL) — estilo BRAdmin
      2) SNMP em 127.0.0.1 (fallback pra laser antigo com Status Monitor)

    Retorna lista de LeituraImpressora (pode ser vazia).
    """
    if os.name != "nt":
        return []

    resultados: List[LeituraImpressora] = []

    # ─── Método 1: direto via SetupAPI (prioridade) ───
    try:
        resultados_direto = ler_via_usb_direto()
        if resultados_direto:
            resultados.extend(resultados_direto)
    except Exception as e:
        log.error("Erro no método USB direto: %s", e)

    # ─── Método 2: SNMP local (só se nada veio do direto) ───
    if not resultados:
        leitura_snmp = tentar_snmp_local()
        if leitura_snmp:
            leitura_snmp.origem = "usb"
            resultados.append(leitura_snmp)
            log.info("Leitura USB via SNMP local OK: serial=%s", leitura_snmp.serial)

    if not resultados:
        # Descobre o estado real pra dar um aviso preciso
        impressoras_wmi = filtrar_brother_usb(listar_impressoras_wmi())
        setup_brothers = 0
        try:
            from coletor import usb_bidi
            setup_brothers = sum(
                1 for d in usb_bidi.enumerar_dispositivos_usbprint()
                if d.get("brother")
            )
        except Exception:
            pass

        if setup_brothers == 0 and impressoras_wmi:
            log.info(
                "Nenhuma Brother USB FISICAMENTE conectada agora "
                "(%d driver(s) instalado(s) no Windows de conexões anteriores, "
                "mas nenhum dispositivo ativo na classe USBPRINT).",
                len(impressoras_wmi)
            )
        elif setup_brothers > 0:
            log.warning(
                "Brother USB ativa(s) (%d) mas nenhum método leu contadores. "
                "Verifique se BRAdmin/iPrint&Scan estão em uso simultâneo, "
                "ou rode --diagnostico-usb.",
                setup_brothers
            )
        else:
            log.info("Nenhuma Brother USB detectada")

    return resultados


# ═══ Diagnóstico ══════════════════════════════════════════════════════════════

def diagnostico_usb() -> dict:
    """Coleta informações cruas para debug. Chamado por --diagnostico-usb."""
    info: dict = {
        "sistema": "windows" if os.name == "nt" else "outro",
        "impressoras_wmi": [],
        "brother_usb_wmi": [],
        "setupapi_dispositivos": [],
        "setupapi_brothers": [],
        "tentativas_pjl": [],
        "snmp_local_respondeu": False,
        "leitura_snmp_local": None,
        "pnp_usb_print": "",
    }

    if os.name != "nt":
        return info

    # 1) Lista completa via WMI
    info["impressoras_wmi"] = listar_impressoras_wmi()
    info["brother_usb_wmi"] = filtrar_brother_usb(info["impressoras_wmi"])

    # 2) Enumeração SetupAPI (método primário)
    try:
        from coletor import usb_bidi
        dispositivos = usb_bidi.enumerar_dispositivos_usbprint()
        info["setupapi_dispositivos"] = dispositivos
        info["setupapi_brothers"] = [d for d in dispositivos if d.get("brother")]

        # 3) Tenta PJL direto em cada Brother
        from coletor.utils import pasta_appdata
        from datetime import datetime
        pasta_dump = pasta_appdata()

        for disp in info["setupapi_brothers"]:
            tent: dict = {
                "instance_id": disp.get("instance_id"),
                "device_path": disp.get("device_path"),
                "bytes_recebidos": 0,
                "parseado": {},
                "amostra_resposta": None,
                "arquivo_dump": None,
                "serial_usb_descriptor": None,
                "erro": None,
            }
            # Fallback: consulta PowerShell pra ver se o USB expõe iSerial
            try:
                tent["serial_usb_descriptor"] = usb_bidi.obter_serial_usb_via_pnp(
                    disp.get("instance_id") or ""
                )
            except Exception as e:
                tent["serial_usb_descriptor"] = f"<erro: {e}>"
            try:
                resposta = usb_bidi.enviar_e_ler_pjl(
                    disp["device_path"], usb_bidi.COMANDO_PJL_INFO
                )
                if resposta:
                    tent["bytes_recebidos"] = len(resposta)
                    tent["parseado"] = usb_bidi.parsear_resposta_pjl(resposta)
                    # Guarda amostra curta no diagnóstico impresso
                    tent["amostra_resposta"] = (
                        resposta[:400].decode("latin-1", errors="replace")
                    )
                    # Salva resposta bruta completa em arquivo (pra analisar serial etc)
                    try:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        vp = (disp.get("vid_pid") or "dev").replace("&", "_")
                        dump_path = pasta_dump / f"pjl_dump_{vp}_{ts}.txt"
                        dump_path.write_bytes(resposta)
                        tent["arquivo_dump"] = str(dump_path)
                    except Exception as e:
                        tent["arquivo_dump"] = f"<erro salvando: {e}>"
                else:
                    tent["erro"] = "sem_resposta"
            except Exception as e:
                tent["erro"] = f"exception: {e}"
            info["tentativas_pjl"].append(tent)
    except Exception as e:
        info["setupapi_dispositivos"] = f"<erro: {e}>"

    # 4) PnP printers (info contextual)
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            "Get-PnpDevice -Class Printer | Select-Object Status,FriendlyName,InstanceId | Format-Table -AutoSize | Out-String",
        ]
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        info["pnp_usb_print"] = (out.stdout or "").strip()
    except Exception as e:
        info["pnp_usb_print"] = f"<erro: {e}>"

    # 5) SNMP local
    try:
        leitura = tentar_snmp_local()
        if leitura:
            info["snmp_local_respondeu"] = True
            info["leitura_snmp_local"] = leitura.to_payload()
    except Exception as e:
        info["leitura_snmp_local"] = f"<erro: {e}>"

    return info


def imprimir_diagnostico_usb() -> None:
    """Imprime o diagnóstico formatado no console."""
    import json

    info = diagnostico_usb()

    print("\n" + "=" * 72)
    print(" DIAGNÓSTICO USB — Sigatec Coletor")
    print("=" * 72)

    if info["sistema"] != "windows":
        print("\nSistema não é Windows. Leitura USB só funciona no Windows.")
        return

    print("\n[1] Impressoras instaladas (Get-Printer):")
    if not info["impressoras_wmi"]:
        print("   (nenhuma)")
    for i, p in enumerate(info["impressoras_wmi"], 1):
        print(f"   [{i}] Nome:   {p.get('nome')}")
        print(f"       Porta:  {p.get('portname')}")
        print(f"       Driver: {p.get('driver')}")

    print("\n[2] Brother USB via filtro WMI:")
    if not info["brother_usb_wmi"]:
        print("   (nenhuma reconhecida — prefixos aceitos:",
              ", ".join(PREFIXOS_PORTA_LOCAL) + ")")
    for p in info["brother_usb_wmi"]:
        print(f"   → {p.get('nome')}  (porta={p.get('portname')})")

    print("\n[3] SetupAPI — dispositivos USBPRINT enumerados:")
    disp = info["setupapi_dispositivos"]
    if isinstance(disp, str):
        print("   " + disp)
    elif not disp:
        print("   (nenhum)")
    else:
        for d in disp:
            marca = "BROTHER ✓" if d.get("brother") else "         "
            vp = d.get("vid_pid") or "?"
            print(f"   [{marca}] {d.get('instance_id')}")
            print(f"             {vp}")
            print(f"             path={d.get('device_path')}")

    print("\n[4] Tentativas de leitura PJL direta (método primário):")
    if not info.get("tentativas_pjl"):
        print("   (nenhuma Brother USBPRINT encontrada)")
    for t in info["tentativas_pjl"]:
        print(f"   Instance: {t.get('instance_id')}")
        print(f"     Bytes recebidos: {t.get('bytes_recebidos')}")
        if t.get("erro"):
            print(f"     ✗ Erro: {t.get('erro')}")
        if t.get("parseado"):
            print(f"     Dados parseados: {json.dumps(t['parseado'], ensure_ascii=False)}")
        sud = t.get("serial_usb_descriptor")
        if sud:
            print(f"     Serial via USB descriptor (PnP): {sud}")
        else:
            print(f"     Serial via USB descriptor (PnP): (não disponível)")
        if t.get("arquivo_dump"):
            print(f"     💾 Resposta completa salva em: {t['arquivo_dump']}")
        if t.get("amostra_resposta"):
            amostra = t["amostra_resposta"].replace("\x1b", "<ESC>").replace("\x0c", "<FF>")
            print(f"     Amostra resposta (400 primeiros bytes):")
            print("       " + amostra[:400].replace("\n", "\n       "))

    print("\n[5] Dispositivos PnP (Get-PnpDevice -Class Printer):")
    print(info["pnp_usb_print"] or "   (vazio)")

    print("\n[6] SNMP 127.0.0.1 (fallback legado):")
    if info["snmp_local_respondeu"]:
        print("   ✓ Respondeu:", json.dumps(info["leitura_snmp_local"], ensure_ascii=False))
    else:
        print("   ✗ Não respondeu (normal pra DCP-T/MFC-J; irrelevante se o método [4] funcionou).")

    print("\n" + "=" * 72)
    print(" Cole a saída acima em uma mensagem para ajuste fino.")
    print("=" * 72 + "\n")
