# Módulo Visualizador de Dispositivos

Plataforma modular de gerenciamento e automação de dispositivos de rede. Atualmente suporta **TVs Roku** via ECP (External Control Protocol), com arquitetura preparada para expansão para outros protocolos e tipos de dispositivos (smart TVs, monitores, IoT, etc).

---

## Visão Geral

O sistema foi projetado com uma arquitetura **plugin-friendly**: cada tipo de dispositivo tem seu próprio módulo de controle, compartilhando uma base comum de configuração, CRUDe visualização. O primeiro módulo implementado é o **Roku ECP**, que cobre:

- Cadastro e gerenciamento de múltiplas TVs via API REST
- Descoberta automática via SSDP
- Watchdog de aplicativo (garante que o app certo esteja na tela)
- Agendamento de ligar/desligar por horário
- Launch com retry e cooldown
- Interface web rotativa (kiosk) para exibição nas TVs

---

## Estrutura

```
visualizador_dispositivos/
├── __init__.py                  # Pacote Python
├── roku_ecp.py                  # Módulo Roku ECP (controlador + watchdog + scheduler)
├── tvs_config.example.json      # Exemplo do banco de configuração das TVs
├── visualizador.example.html    # Exemplo da página HTML exibida nas TVs
├── kiosk-roku/                  # App BrightScript nativo para TVs Roku
│   ├── source/main.brs
│   ├── components/MainScene.xml
│   ├── components/MainScene.brs
│   ├── images/icon.png
│   └── manifest
├── LICENSE                      # GPLv3
└── README.md
```

> **Atenção:** `tvs_config.json` e `visualizador.html` contêm IPs internos da rede e foram adicionados ao `.gitignore`. Use os arquivos `.example` como base.

---

## Protocolos Suportados (Atual / Planejado)

| Protocolo | Tipo de Dispositivo | Status |
|---|---|---|
| **Roku ECP** (HTTP) | TVs Roku | Implementado |
| **MQTT** | Dispositivos IoT / domótica | Planejado |
| **ONVIF** | Câmeras / monitores IP | Planejado |
| **HTTP/REST genérico** | Qualquer device com API | Planejado |
| **SNMP** | Switches / roteadores / impressoras | Planejado |

---

## Como Instalar

### Passo 1: Copiar a Pasta
Copie `modulos/visualizador_dispositivos` para a pasta `modulos` do seu projeto FastAPI.

### Passo 2: Renomear os Exemplos
- `tvs_config.example.json` → **`tvs_config.json`**
- `visualizador.example.html` → **`visualizador.html`**

Preencha com os IPs da sua rede.

### Passo 3: Importar no `main.py`

```python
from fastapi import FastAPI
from modulos.visualizador_dispositivos import roku_ecp

app = FastAPI()

# Registra os endpoints Roku
app.include_router(roku_ecp.roku_router)

@app.on_event("startup")
async def startup_event():
    await roku_ecp.start_roku_watcher()
```

---

## Endpoints Disponíveis (Roku)

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/roku/tvs` | Lista TVs e status |
| `POST` | `/api/roku/tvs` | Cadastra TV |
| `PUT` | `/api/roku/tvs/{id}` | Edita configurações |
| `DELETE` | `/api/roku/tvs/{id}` | Remove TV |
| `POST` | `/api/roku/tvs/{id}/launch` | Força abertura do app |
| `POST` | `/api/roku/tvs/{id}/power-on` | Liga a TV |
| `POST` | `/api/roku/tvs/{id}/power-off` | Desliga a TV |
| `GET` | `/api/roku/tvs/{id}/status` | Status detalhado |
| `GET` | `/api/roku/tvs/{id}/apps` | Lista apps instalados |
| `GET` | `/api/roku/visualizador` | Serve o HTML do kiosk |

---

## TVs Roku (App Nativo)

Para instalação nova, instale o app em `kiosk-roku/` nas TVs Roku habilitando o **Developer Mode**. O `visualizador.html` gerado pelo sistema é a página que o app da kiosk abre na TV.

---

## Roadmap

- [ ] Módulo MQTT para dispositivos IoT
- [ ] Módulo ONVIF para câmeras/monitores
- [ ] Dashboard web de gerenciamento (além do kiosk)
- [ ] Alertas e notificações (webhook, e-mail)
- [ ] Histórico de eventos e logs
- [ ] Suporte a múltiplos protocols em paralelo

---

## Licença

GNU General Public License v3.0 — veja [LICENSE](LICENSE).
