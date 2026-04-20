# Arquitetura — Sigatec Coletor Pro

## Visão Geral

```
┌─────────────────────────────────────────────────────────────┐
│                   SIGATEC SERVIDOR (Railway)                 │
│                                                             │
│   FastAPI /api/bc/tunnel/ws  ←── WebSocket persistente ──┐  │
│   GET /api/bc/tunnel/agents  ←── lista REST admin        │  │
│   GET /api/bc/tunnel/proxy/{id}/{ip}/{path}               │  │
│                      ↑ HTTP do browser                    │  │
│                      ↓ proxy_request via WS               │  │
└───────────────────────────────────────────────────────────┼──┘
                                                            │
                                         WebSocket (wss://)│
                                                            │
┌───────────────────────────────────────────────────────────┼──┐
│                 SIGATEC COLETOR PRO (PC do cliente)       │  │
│                                                            │  │
│  main.py ──→ AppColetor (Tkinter)                         │  │
│               ├── Agendador (thread)   ← coleta/envio     │  │
│               ├── TunnelClient (thread) ──────────────────┘  │
│               │    ├── heartbeat (30s)                        │
│               │    ├── proxy_request handler                  │
│               │    └── printer_list sync                      │
│               └── IconeBandeja (pystray)                      │
│                                                               │
│  Rede local:  PC ──SNMP──→ Brother Printer                   │
│               PC ←─HTTP──  Printer Web Panel                 │
└───────────────────────────────────────────────────────────────┘
```

## Protocolo do Túnel (JSON sobre WebSocket)

### Agente → Servidor

| Tipo | Quando | Campos |
|------|--------|--------|
| `heartbeat` | A cada 30s | `installation_id`, `timestamp` |
| `printer_list` | Ao conectar e quando mudar | `installation_id`, `agent_name`, `printers[]` |
| `proxy_response` | Em resposta a `proxy_request` | `id`, `status`, `headers`, `body` (base64), `content_type` |
| `pong` | Em resposta a `ping` | — |

### Servidor → Agente

| Tipo | Quando | Campos |
|------|--------|--------|
| `proxy_request` | Browser abre painel | `id`, `ip`, `path`, `method`, `headers` |
| `ping` | Keepalive | — |

## Fluxo de Acesso Remoto

1. Admin abre aba **Acesso Remoto** no frontend
2. Frontend chama `GET /api/bc/tunnel/agents` → lista de agentes + impressoras
3. Admin clica "Acessar Painel" em uma impressora
4. Browser abre `GET /api/bc/tunnel/proxy/{installation_id}/{ip}/`
5. Servidor envia `proxy_request` via WS ao agente
6. Agente faz `GET http://{ip}/` na rede local
7. Agente reescreve URLs no HTML (links relativos → proxy URLs)
8. Agente devolve `proxy_response` via WS
9. Servidor retorna HTML ao browser

## Segurança

- Autenticação WS: `X-API-Key` (INGEST_API_KEY ou API_KEY master)
- Endpoints REST de tunnel: `require_admin` (JWT admin ou API_KEY master)
- Sem abertura de portas no cliente
- Proxy sem execução de código — apenas HTTP GET forwarding
- Body em base64 evita encoding issues
