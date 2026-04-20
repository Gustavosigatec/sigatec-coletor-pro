# Sigatec Coletor Pro

## O que é
Versão expandida do Sigatec Coletor que faz tudo que o original faz (varredura de rede/USB + coleta de contadores de impressoras Brother + envio automático) MAIS acesso remoto ao painel web das impressoras via túnel WebSocket reverso.

## Dois produtos
- **Sigatec Coletor** (light): C:\sigatec-coletor — só coleta contadores. Não mexer.
- **Sigatec Coletor Pro** (este projeto): coleta + acesso remoto. Substitui o light quando necessário.

## Integração com brother-counter
O brother-counter (C:\brother-counter) é o sistema de gestão central rodando no Railway (https://sigatec-sistema-production.up.railway.app/). O Coletor Pro se integra com ele:
- Envia contadores via POST /api/bc/ingest (mesmo que o Coletor light)
- Mantém WebSocket aberto com /api/bc/tunnel/connect para acesso remoto
- Frontend do brother-counter terá botão "Acessar painel" que usa /api/bc/tunnel/proxy/{installation_id}/{ip}/{path}

## Stack
- Python 3.10+, Windows 10/11
- Tkinter + pystray (bandeja do sistema)
- PyInstaller (exe único)
- puresnmp 1.11.0 (varredura SNMP)
- websockets >= 12.0 (túnel)
- pywin32 (USB)

## Estrutura base (herdada do sigatec-coletor)
```
sigatec-coletor-pro/
├── main.py                 # Entry point
├── coletor/
│   ├── config.py           # Config + API key ofuscada
│   ├── api_client.py       # POST /api/bc/ingest
│   ├── snmp_reader.py      # Varredura SNMP rede
│   ├── usb_reader.py       # Coleta USB
│   ├── usb_bidi.py         # PJL + SetupAPI
│   ├── agendador.py        # Scheduler
│   ├── tunnel.py           # NOVO: WebSocket tunnel reverso
│   ├── ui.py               # GUI + bandeja
│   ├── windows_startup.py  # Autostart
│   └── utils.py            # Logger, paths
├── servidor/
│   └── endpoint_coletor.py
│   └── tunnel_router.py    # NOVO: router FastAPI para tunnel
├── installer/
│   └── build.py
└── docs/
```

## Autenticação
- Coletor → Servidor: Header X-API-Key + X-Installation-ID
- Chave ofuscada no exe via XOR + Base64
- Build: python installer/build.py --ingest-key "chave"

## Arquitetura do túnel
1. Coletor Pro conecta via WebSocket em /api/bc/tunnel/connect
2. Servidor registra installation_id → ws_connection
3. Usuário clica "Acessar painel" no brother-counter
4. GET /api/bc/tunnel/proxy/{installation_id}/{ip}/{path}
5. Servidor envia request via WS pro Coletor
6. Coletor faz HTTP GET local na impressora e devolve via WS
7. Servidor retorna pro navegador do usuário

## Desafios conhecidos
- Rewrite de HTML (paths absolutos do painel da impressora)
- Static resources (CSS/JS) precisam passar pelo proxy
- Cookies de sessão da impressora
- Reconexão automática do WebSocket se cair

## Config runtime
%APPDATA%\SigatecColetorPro\config.json
Campos novos vs Coletor: tunnel.ativo, tunnel.ws_url

## Dados coletados por impressora
serial, modelo, contagem_paginas, contador_mono, contador_color, toner (KCMY 0-100%), ip, tipo_conexao

## Endpoint ingest (formato)
POST /api/bc/ingest
Header: X-API-Key, X-Installation-ID
Body: {"registros": [{"date", "serial", "modelo", "contagem_paginas", "folder": "AGENT_LOCAL", "from", "subject", ...}]}
