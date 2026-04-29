"""
Comunicação USB bidirecional com impressoras Brother (modo BRAdmin).

Abre a porta USBPRINT diretamente via SetupAPI + CreateFile, enviando
comandos PJL e lendo a resposta de volta pelo mesmo canal — sem passar
pelo spooler do Windows. É a forma confiável de ler contadores de
impressoras Brother DCP-T / MFC-J (jato) via USB.

Requer: pywin32 (para win32file/win32api) e Windows.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes, byref, sizeof, Structure, POINTER, c_void_p
from typing import List, Optional

from coletor.utils import get_logger

log = get_logger()


# ═══ GUIDs / constantes Win32 ═════════════════════════════════════════════════

# GUID_DEVINTERFACE_USBPRINT (a classe de interface do Windows para impressoras USB)
# {28d78fad-5a12-11d1-ae5b-0000f803a8c2}
_USBPRINT_GUID_BYTES = (0x28d78fad, 0x5a12, 0x11d1,
                        (0xae, 0x5b, 0x00, 0x00, 0xf8, 0x03, 0xa8, 0xc2))

DIGCF_PRESENT           = 0x02
DIGCF_DEVICEINTERFACE   = 0x10

GENERIC_READ            = 0x80000000
GENERIC_WRITE           = 0x40000000
FILE_SHARE_READ         = 0x01
FILE_SHARE_WRITE        = 0x02
OPEN_EXISTING           = 3
INVALID_HANDLE_VALUE    = -1

ERROR_NO_MORE_ITEMS          = 259
ERROR_INSUFFICIENT_BUFFER    = 122
ERROR_SHARING_VIOLATION      = 32
ERROR_ACCESS_DENIED          = 5


# ═══ Estruturas C ═════════════════════════════════════════════════════════════

class _GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", c_void_p),
    ]


class _SP_DEVINFO_DATA(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", c_void_p),
    ]


def _construir_usbprint_guid() -> _GUID:
    g = _GUID()
    g.Data1 = _USBPRINT_GUID_BYTES[0]
    g.Data2 = _USBPRINT_GUID_BYTES[1]
    g.Data3 = _USBPRINT_GUID_BYTES[2]
    for i, b in enumerate(_USBPRINT_GUID_BYTES[3]):
        g.Data4[i] = b
    return g


# ═══ Bindings ctypes (lazy — só carrega no Windows) ═══════════════════════════

_setupapi = None
_kernel32 = None


def _carregar_libs():
    global _setupapi, _kernel32
    if _setupapi is not None:
        return
    if os.name != "nt":
        raise RuntimeError("usb_bidi só funciona no Windows")

    _setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _setupapi.SetupDiGetClassDevsW.argtypes = [
        POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
    ]
    _setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE

    _setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, POINTER(_SP_DEVINFO_DATA), POINTER(_GUID),
        wintypes.DWORD, POINTER(_SP_DEVICE_INTERFACE_DATA),
    ]
    _setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

    _setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, POINTER(_SP_DEVICE_INTERFACE_DATA),
        c_void_p, wintypes.DWORD, POINTER(wintypes.DWORD),
        POINTER(_SP_DEVINFO_DATA),
    ]
    _setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

    _setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        wintypes.HANDLE, POINTER(_SP_DEVINFO_DATA),
        wintypes.LPWSTR, wintypes.DWORD, POINTER(wintypes.DWORD),
    ]
    _setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL

    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    _setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE

    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, c_void_p, wintypes.DWORD,
        POINTER(wintypes.DWORD), c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, c_void_p, wintypes.DWORD,
        POINTER(wintypes.DWORD), c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


# ═══ API pública ══════════════════════════════════════════════════════════════

def enumerar_dispositivos_usbprint() -> List[dict]:
    """Lista dispositivos Windows na classe USBPRINT.

    Cada item: {"device_path": "\\\\?\\USB#VID_...", "instance_id": "USBPRINT\\...", "brother": bool}
    """
    if os.name != "nt":
        return []

    try:
        _carregar_libs()
    except Exception as e:
        log.warning("Falha carregando SetupAPI: %s", e)
        return []

    guid = _construir_usbprint_guid()
    hdev = _setupapi.SetupDiGetClassDevsW(
        byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if hdev == INVALID_HANDLE_VALUE or hdev == 0:
        log.warning("SetupDiGetClassDevsW falhou: %d", ctypes.get_last_error())
        return []

    dispositivos: List[dict] = []
    try:
        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = sizeof(_SP_DEVICE_INTERFACE_DATA)
            ok = _setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, byref(guid), index, byref(iface)
            )
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_NO_MORE_ITEMS:
                    break
                log.debug("SetupDiEnumDeviceInterfaces erro %d", err)
                break

            # Descobre o tamanho do buffer
            tamanho_buffer = wintypes.DWORD(0)
            _setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, byref(iface), None, 0, byref(tamanho_buffer), None
            )
            if tamanho_buffer.value == 0:
                index += 1
                continue

            # Layout de SP_DEVICE_INTERFACE_DETAIL_DATA_W:
            #   DWORD cbSize;          // offset 0, 4 bytes
            #   WCHAR DevicePath[1];   // offset 4, null-terminated
            # cbSize a escrever: 6 em 32-bit, 8 em 64-bit (alinhamento de ponteiro)
            ptr_size = ctypes.sizeof(c_void_p)
            cb_size_header = 6 if ptr_size == 4 else 8

            buf = ctypes.create_string_buffer(tamanho_buffer.value)
            # Escreve cbSize nos primeiros 4 bytes
            ctypes.cast(buf, POINTER(wintypes.DWORD))[0] = cb_size_header

            devinfo = _SP_DEVINFO_DATA()
            devinfo.cbSize = sizeof(_SP_DEVINFO_DATA)

            required = wintypes.DWORD(0)
            ok = _setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, byref(iface),
                ctypes.cast(buf, c_void_p), tamanho_buffer.value,
                byref(required), byref(devinfo)
            )
            if not ok:
                log.debug("GetDeviceInterfaceDetail falhou: %d", ctypes.get_last_error())
                index += 1
                continue

            # DevicePath é a string wide começando no offset 4 (logo após cbSize)
            device_path = ctypes.wstring_at(ctypes.addressof(buf) + 4)

            # Agora lê o InstanceId pra saber se é Brother
            instance_id = ""
            tam_inst = wintypes.DWORD(512)
            buf_inst = ctypes.create_unicode_buffer(tam_inst.value)
            ok2 = _setupapi.SetupDiGetDeviceInstanceIdW(
                hdev, byref(devinfo), buf_inst, tam_inst, byref(tam_inst)
            )
            if ok2:
                instance_id = buf_inst.value

            s_busca = ((instance_id or "") + " " + (device_path or "")).upper()
            # Brother = string "BROTHER" ou VID USB-IF 04F9
            eh_brother = ("BROTHER" in s_busca) or ("VID_04F9" in s_busca)

            dispositivos.append({
                "device_path": device_path,
                "instance_id": instance_id,
                "brother": eh_brother,
                "vid_pid": _extrair_vid_pid(s_busca),
            })

            index += 1
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(hdev)

    return dispositivos


def _extrair_vid_pid(texto: str) -> str:
    """Extrai 'VID_XXXX&PID_XXXX' do caminho se presente, pra log."""
    import re
    m = re.search(r'VID_([0-9A-F]{4})[^A-Z0-9]*PID_([0-9A-F]{4})', texto, re.IGNORECASE)
    return f"VID_{m.group(1).upper()}&PID_{m.group(2).upper()}" if m else ""


def enviar_e_ler_pjl(device_path: str,
                     comando_pjl: bytes,
                     tamanho_leitura: int = 4096,
                     pausa_segundos: float = 0.4,
                     timeout_leitura_segundos: float = 3.0) -> Optional[bytes]:
    """Abre a porta USBPRINT, envia os bytes PJL e lê a resposta.

    Retorna os bytes recebidos ou None se algo falhar.

    Estratégia:
      1. CreateFileW com GENERIC_READ | GENERIC_WRITE
      2. WriteFile (envia o pacote PJL completo)
      3. Sleep curto pro firmware processar
      4. ReadFile em loop até lim de tempo, acumulando resposta
    """
    if os.name != "nt":
        return None

    try:
        _carregar_libs()
    except Exception as e:
        log.warning("Falha carregando libs: %s", e)
        return None

    # Aceita compartilhar leitura/escrita com o driver (senão pode dar sharing violation)
    handle = _kernel32.CreateFileW(
        device_path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle == 0:
        err = ctypes.get_last_error()
        log.warning("CreateFileW falhou em %s: erro %d", device_path, err)
        return None

    try:
        # Escrita — usa buffer ctypes explícito pra garantir ponteiro válido
        tx_buf = (ctypes.c_ubyte * len(comando_pjl)).from_buffer_copy(comando_pjl)
        escritos = wintypes.DWORD(0)
        ok = _kernel32.WriteFile(
            handle, tx_buf, len(comando_pjl), byref(escritos), None
        )
        if not ok:
            err = ctypes.get_last_error()
            log.warning("WriteFile falhou: erro %d", err)
            return None
        log.debug("PJL enviado: %d bytes", escritos.value)

        time.sleep(pausa_segundos)

        # Leitura em loop
        resposta = bytearray()
        inicio = time.monotonic()
        while (time.monotonic() - inicio) < timeout_leitura_segundos:
            buf = (ctypes.c_ubyte * tamanho_leitura)()
            lidos = wintypes.DWORD(0)
            ok = _kernel32.ReadFile(
                handle, buf, tamanho_leitura, byref(lidos), None
            )
            if not ok:
                break
            if lidos.value == 0:
                # Pequena pausa e tenta mais uma vez
                time.sleep(0.1)
                if resposta:
                    break
                continue
            resposta.extend(bytes(buf[:lidos.value]))
            # Heurística: se recebemos o UEL de fim, paramos
            if b"\x1b%-12345X" in bytes(resposta[-32:]):
                break

        return bytes(resposta) if resposta else None
    finally:
        _kernel32.CloseHandle(handle)


# ═══ PJL: montagem do pacote + parser ═════════════════════════════════════════

UEL = b"\x1b%-12345X"  # Universal Exit Language

# Comandos PJL enviados pra impressora. Evita-se INFO VARIABLES (inunda com
# dezenas de linhas de capacidades do driver e estoura o buffer antes dos outros
# comandos responderem). Probes focadas em identificação:
COMANDO_PJL_INFO = (
    UEL +
    b"@PJL\r\n"
    b"@PJL INFO ID\r\n"
    b"@PJL INFO PAGECOUNT\r\n"
    b"@PJL INFO SERIALNUMBER\r\n"
    b"@PJL INFO CONFIG\r\n"
    b"@PJL INFO STATUS\r\n"
    b"@PJL INFO USTATUS DEVICE\r\n"
    b"@PJL INFO BRDEVSTATUS\r\n"
    b"@PJL INFO BRCOUNT\r\n"
    b"@PJL INFO BRSERIAL\r\n"
    b"@PJL INFO BRMACHINE\r\n"
    b"@PJL INFO MACHINFO\r\n"
    b"@PJL INFO PRODINFO\r\n"
    b"@PJL DINQUIRE SERIALNUMBER\r\n"
    b"@PJL DINQUIRE MACHINESERIAL\r\n"
    b"@PJL EOJ\r\n" +
    UEL
)


def parsear_resposta_pjl(resposta: bytes) -> dict:
    """Extrai campos úteis da resposta PJL.

    Retorna dicionário com chaves que podem estar presentes:
      serial, modelo, pagecount, pagecount_mono, pagecount_color,
      ink_preto, ink_ciano, ink_magenta, ink_amarelo, status
    """
    import re

    if not resposta:
        return {}

    try:
        texto = resposta.decode("latin-1", errors="ignore")
    except Exception:
        texto = ""

    dados: dict = {}

    def achar(regex: str, flags=0) -> Optional[str]:
        m = re.search(regex, texto, flags | re.IGNORECASE)
        return m.group(1).strip().strip('"') if m else None

    # Modelo (INFO ID retorna a string do modelo)
    # Brother costuma devolver "Brother DCP-T730DW:8CH-A47-001:Ver.1.09"
    # → queremos só "Brother DCP-T730DW" (parte antes do primeiro :)
    m = re.search(r'@PJL\s+INFO\s+ID\s*\r?\n\s*"?([^\r\n"]+?)"?\s*\r?\n',
                  texto, re.IGNORECASE)
    if m:
        modelo_raw = m.group(1).strip()
        dados["modelo_raw"] = modelo_raw
        # Corta no primeiro ":" (tira código PCB e versão firmware)
        modelo_limpo = modelo_raw.split(":")[0].strip()
        # Tira "Brother " do começo pra ficar só o modelo (padrão usado no projeto)
        modelo_limpo = re.sub(r"^Brother\s+", "", modelo_limpo, flags=re.IGNORECASE)
        dados["modelo"] = modelo_limpo

    # PAGECOUNT
    m = re.search(r'PAGECOUNT\s*=\s*(\d+)', texto, re.IGNORECASE)
    if m:
        dados["pagecount"] = int(m.group(1))

    # SERIAL — tentamos vários formatos em ordem de confiabilidade
    # A resposta PJL típica é:
    #   @PJL INFO SERIALNUMBER
    #   "ABCD1234"         ← aspas em linha separada
    # Ou:
    #   @PJL DINQUIRE SERIALNUMBER
    #   SERIALNUMBER="ABCD1234"
    #
    # Valor inválido pra ignorar: "?" (impressora não conhece)
    def _eh_serial_valido(s: str) -> bool:
        if not s:
            return False
        s = s.strip().strip('"').strip()
        return bool(s) and s != "?" and len(s) >= 4 and bool(re.match(r'^[A-Z0-9\-]+$', s, re.IGNORECASE))

    candidatos_serial = []

    # 1) Formato "bloco": @PJL INFO|DINQUIRE SERIALNUMBER + linha seguinte com "VALOR"
    for m in re.finditer(
        r'@PJL\s+(?:INFO|DINQUIRE)\s+(?:SERIALNUMBER|MACHINESERIAL|BRSERIAL|PRODSERIAL)\s*\r?\n\s*"?([^\r\n"]*)"?\s*\r?\n',
        texto, re.IGNORECASE
    ):
        candidatos_serial.append(m.group(1).strip().strip('"'))

    # 2) Formato KEY=VALUE
    for pat in [r'SERIALNUMBER\s*=\s*"?([^\r\n"]+?)"?\s*(?:\r|\n|$)',
                r'MACHINESERIAL\s*=\s*"?([^\r\n"]+?)"?\s*(?:\r|\n|$)',
                r'BRSERIAL\s*=\s*"?([^\r\n"]+?)"?\s*(?:\r|\n|$)',
                r'PRODSERIAL\s*=\s*"?([^\r\n"]+?)"?\s*(?:\r|\n|$)',
                r'SERIALNO\s*=\s*"?([^\r\n"]+?)"?\s*(?:\r|\n|$)',
                r'MFG_SN\s*=\s*"?([^\r\n"]+?)"?\s*(?:\r|\n|$)']:
        for m in re.finditer(pat, texto, re.IGNORECASE):
            candidatos_serial.append(m.group(1).strip().strip('"'))

    for cand in candidatos_serial:
        if _eh_serial_valido(cand):
            dados["serial"] = cand.upper()
            break

    # Brother BRCOUNT (retorna vários contadores, formato multi-linha)
    m = re.search(r'TOTALPAGE\s*=\s*(\d+)', texto, re.IGNORECASE)
    if m and "pagecount" not in dados:
        dados["pagecount"] = int(m.group(1))
    m = re.search(r'PRINTPAGE\s*=\s*(\d+)', texto, re.IGNORECASE)
    if m and "pagecount" not in dados:
        dados["pagecount"] = int(m.group(1))

    # Mono / Color (laser colorida)
    m = re.search(r'MONO(?:CHROME)?PAGE\s*=\s*(\d+)', texto, re.IGNORECASE)
    if m:
        dados["pagecount_mono"] = int(m.group(1))
    m = re.search(r'COLOR(?:PAGE)?\s*=\s*(\d+)', texto, re.IGNORECASE)
    if m:
        dados["pagecount_color"] = int(m.group(1))

    # Níveis de tinta (jato) ou toner (laser) em INFO VARIABLES
    # Formato varia por modelo; tentamos vários padrões
    for cor, chaves in [
        ("preto",   ["BLACKINKLIFE", "BLACKTONER", "INKLIFE:BK", "KTONERLIFE"]),
        ("ciano",   ["CYANINKLIFE", "CYANTONER", "INKLIFE:C", "CTONERLIFE"]),
        ("magenta", ["MAGENTAINKLIFE", "MAGENTATONER", "INKLIFE:M", "MTONERLIFE"]),
        ("amarelo", ["YELLOWINKLIFE", "YELLOWTONER", "INKLIFE:Y", "YTONERLIFE"]),
    ]:
        for chave in chaves:
            padrao = re.escape(chave).replace("\\:", "[:=]") + r"\s*=?\s*(\d+)"
            m = re.search(padrao, texto, re.IGNORECASE)
            if m:
                pct = int(m.group(1))
                if 0 <= pct <= 100:
                    dados[f"ink_{cor}"] = pct
                break

    # Status (útil pra log)
    m = re.search(r'@PJL\s+INFO\s+STATUS\s*\r?\n([^\x0c]+?)(?:\x0c|$)',
                  texto, re.IGNORECASE | re.DOTALL)
    if m:
        dados["status"] = m.group(1).strip()[:256]

    return dados


# ═══ Fallback: consulta USB descriptor via PowerShell ═════════════════════════

def obter_serial_usb_via_pnp(instance_id: str) -> Optional[str]:
    """Tenta extrair o serial real do USB descriptor via Get-PnpDeviceProperty.

    O dispositivo que enumeramos (USB\\VID_xxx&PID_xxx&MI_00\\...) é a interface 0.
    O **dispositivo pai** (composite USB) costuma ter como InstanceId o próprio
    iSerialNumber — SE o firmware do dispositivo expuser um serial no descriptor
    USB. Muitas Brother DCP-T fazem isso.

    Formato esperado do pai: USB\\VID_04F9&PID_0719\\E12345678
                                                    ^^^^^^^^^ serial

    Se o pai tiver um ID sintetizado pelo Windows (começa com dígito&), retorna None.

    Fallback: se PowerShell falhar (Win 7 com PS 2.0 nao tem Get-PnpDeviceProperty),
    usa registry diretamente.
    """
    import subprocess
    import re as _re

    if os.name != "nt" or not instance_id:
        return None

    # ── Tentativa 1: PowerShell Get-PnpDeviceProperty (Win 8.1+/Win 10+) ─────
    cmd_ps = (
        f"(Get-PnpDeviceProperty -InstanceId '{instance_id}' "
        f"-KeyName 'DEVPKEY_Device_Parent').Data"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd_ps],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        parent_id = (out.stdout or "").strip()
        stderr_text = (out.stderr or "").strip()
        log.debug("Parent device do %s: %s", instance_id, parent_id)

        # Se PowerShell nao reconhecer o cmdlet (Win 7 PS 2.0), parent_id vem vazio
        # ou stderr tem "CommandNotFoundException" — cai no fallback
        if parent_id and "CommandNotFoundException" not in stderr_text:
            m = _re.search(
                r'USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}\\([^\s\\]+)',
                parent_id, _re.IGNORECASE
            )
            if m:
                candidato = m.group(1).strip()
                if _re.match(r'^\d+&', candidato):
                    log.debug("Parent ID sintetizado por Windows (sem iSerial real): %s", candidato)
                else:
                    return candidato.upper()
    except Exception as e:
        log.debug("Erro consultando parent do USB via PowerShell: %s", e)

    # ── Tentativa 2: Registry direto (compativel com Win 7 SP1+) ─────────────
    return _obter_serial_usb_via_registry(instance_id)


def _obter_serial_usb_via_registry(instance_id: str) -> Optional[str]:
    """Le o serial USB diretamente do registry do Windows.

    Path: HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB\\VID_xxxx&PID_xxxx\\<SERIAL>

    Os subkeys diretos de VID_xxxx&PID_xxxx sao OS SERIAIS REAIS dos dispositivos
    (ou IDs sintetizados pelo Windows quando o firmware nao tem iSerialNumber).

    Estrategia:
      1. Extrai VID e PID do instance_id (ex: USB\\VID_04F9&PID_0719&MI_00\\...)
      2. Abre HKLM\\...Enum\\USB\\VID_xxxx&PID_xxxx (sem &MI_xx — chave do device pai)
      3. Enumera subkeys e retorna o primeiro que NAO seja sintetizado.
    """
    import re as _re

    if os.name != "nt" or not instance_id:
        return None

    try:
        import winreg  # noqa: F401  (so existe no Windows)
    except ImportError:
        return None

    # Extrai VID e PID do instance_id
    m = _re.search(r'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})', instance_id, _re.IGNORECASE)
    if not m:
        return None
    vid, pid = m.group(1).upper(), m.group(2).upper()

    chave_pai = rf"SYSTEM\CurrentControlSet\Enum\USB\VID_{vid}&PID_{pid}"

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, chave_pai) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                # Filtra IDs sintetizados pelo Windows (digit& = sem iSerial real)
                if _re.match(r'^\d+&', sub):
                    log.debug("Subkey USB sintetizado (ignorado): %s", sub)
                    continue
                # Tem cara de serial real
                log.info("Serial obtido do registry USB (Win 7 fallback): %s", sub)
                return sub.upper()
    except FileNotFoundError:
        log.debug("Chave de registry nao encontrada: %s", chave_pai)
    except OSError as e:
        log.debug("Erro lendo registry USB: %s", e)
    return None


# ═══ Helper: extrai serial/nome da InstanceId ═════════════════════════════════

def info_da_instance_id(instance_id: str) -> dict:
    """Tenta extrair modelo e um "hint" de serial de uma Instance ID do Windows.

    Ex: 'USBPRINT\\BROTHERDCP-T730DW\\7&A7C6F67&0&USB004'
        → modelo: 'DCP-T730DW'
        → porta USB: 'USB004'
    """
    import re
    info = {}
    if not instance_id:
        return info

    # Modelo: entre \BROTHER e \
    m = re.search(r'\\BROTHER([A-Z0-9\-]+)', instance_id, re.IGNORECASE)
    if m:
        info["modelo_hint"] = m.group(1).strip().upper()

    # Porta USB no final
    m = re.search(r'(USB\d+)', instance_id, re.IGNORECASE)
    if m:
        info["porta_usb"] = m.group(1).upper()

    return info
