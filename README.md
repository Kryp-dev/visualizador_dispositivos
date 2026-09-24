# Módulo Visualizador de Dispositivos

Plataforma modular de gerenciamento e automação de dispositivos de rede. Atualmente suporta **TVs Roku** via ECP (External Control Protocol) e **TVs LG webOS** via protocolo nativo (SSAP), com arquitetura preparada para expansão para outros protocolos e tipos de dispositivos (smart TVs, monitores, IoT, etc).

---

## Visão Geral

O sistema foi projetado com uma arquitetura **plugin-friendly**: cada tipo de dispositivo tem seu próprio módulo de controle, compartilhando uma base comum de configuração, CRUD e visualização. Os módulos atuais incluem **Roku ECP** e **LG webOS**, que cobrem:

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
├── webos_driver.py              # Módulo LG webOS (controlador + watchdog + scheduler)
├── painel.html                  # Página de gerenciamento (admin) das TVs
├── tvs_config.example.json      # Exemplo do banco de configuração das TVs
├── visualizador.example.html    # Exemplo da página HTML exibida nas TVs
├── kiosk-roku/                  # App BrightScript nativo para TVs Roku
├── kiosk-webos/                 # App nativo HTML/JS para TVs LG webOS
│   ├── appinfo.json
│   ├── index.html
│   ├── config.js
│   ├── icon.png
│   ├── largeIcon.png
│   ├── bgImage.png
│   └── package.bat              # Script para gerar o IPK de instalação
├── LICENSE                      # GPLv3
└── README.md
```

> **Atenção:** Copie os arquivos `.example` e renomeie-os para os nomes oficiais (`tvs_config.json` e `visualizador.html`), depois preencha com os IPs da sua rede.

---

## Protocolos Suportados (Atual / Planejado)

| Protocolo | Tipo de Dispositivo | Status |
|---|---|---|
| **Roku ECP** (HTTP) | TVs Roku | Implementado |
| **LG webOS** (SSAP/WSS) | TVs LG Smart | Implementado |
| **MQTT** | Dispositivos IoT / domótica | Planejado |
| **ONVIF** | Câmeras / monitores IP | Planejado |
| **HTTP/REST genérico** | Qualquer device com API | Planejado |
| **SNMP** | Switches / roteadores / impressoras | Planejado |

---

## Como Instalar

### Passo 1: Configurar os Arquivos de Exemplo
Copie e renomeie:
- `tvs_config.example.json` → **`tvs_config.json`**
- `visualizador.example.html` → **`visualizador.html`**

Preencha com os IPs da sua rede.

### Passo 2: Importar no `main.py`

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
| `GET` | `/api/roku/visualizador` | Serve o HTML do kiosk (tela das TVs) |
| `GET` | `/api/roku/painel` | Serve o painel de gerenciamento (admin) |

O **painel de gerenciamento** (cadastrar/editar TVs, botões liga/desliga/app e agenda semanal) está pronto e acompanha o módulo. Após subir o servidor, acesse `http://IP:PORTA/api/roku/painel`.

---

## Agendamento por Dia da Semana

Cada TV aceita uma lista de **regras de horário** no campo `schedules`. Cada regra define os dias da semana (`days`) e os horários de ligar (`on`) e desligar (`off`):

```json
{
  "id": "tv-01",
  "nome": "TV SERRALHERIA",
  "ip": "192.168.0.100",
  "channel_id": "880042",
  "watchdog": true,
  "enabled": true,
  "schedules": [
    { "days": [0, 1, 2, 3], "on": "06:50", "off": "17:55" },
    { "days": [4],           "on": "07:30", "off": "16:00" }
  ]
}
```

**Dias da semana** seguem o padrão do Python (`datetime.weekday()`):

| Número | Dia | Alternativa em texto |
|---|---|---|
| 0 | Segunda | `segunda`, `mon` |
| 1 | Terça | `terca`, `tue` |
| 2 | Quarta | `quarta`, `wed` |
| 3 | Quinta | `quinta`, `thu` |
| 4 | Sexta | `sexta`, `fri` |
| 5 | Sábado | `sabado`, `sat` |
| 6 | Domingo | `domingo`, `sun` |

Regras:

- **Dias sem regra ficam desligados** o dia inteiro — no exemplo acima, sábado e domingo a TV permanece apagada (não há regra para os dias 5 e 6).
- Uma TV pode ter **várias regras para o mesmo dia** (ex.: pausa no almoço — liga 06:50, desliga 12:00, liga 13:00, desliga 17:55).
- `days` vazio (`[]`) significa **todos os dias**.
- Horários de `off` antes de `on` cruzam a meia-noite (funcionamento noturno).

**Compatibilidade:** o formato antigo `schedule_on` / `schedule_off` segue funcionando e vale para todos os dias. O sistema usa `schedules` quando presente; caso contrário, cai no formato legado. TVs sem nenhum horário configurado ficam sempre ativas (gerenciadas apenas pelo watchdog).

---

## URL da Imagem (Kiosk) — Parametrizada

O app kiosk (`kiosk-roku/`) exibe uma imagem gerada pelo servidor. A URL **não é mais fixa no código**; ela é resolvida nesta ordem:

1. **`image_url`** configurada na TV (`tvs_config.json` ou painel → campo "URL da imagem") é injetada no launch via ECP deep-link (`contentId`) — recomendado, permite uma URL por TV;
2. **`ROKU_SNAPSHOT_URL`** (variável de ambiente do servidor) — valor padrão para todas as TVs quando o campo individual está vazio;
3. **`snapshot_url`** no `manifest` do app Roku — fallback quando o app abre sem o ECP (ex.: lançado manualmente pelo controle).

```bash
# Linux/Mac (systemd, .env ou export)
export ROKU_SNAPSHOT_URL="http://192.168.0.100:8080/api/snapshot.png"
```

```powershell
# Windows PowerShell
$env:ROKU_SNAPSHOT_URL = "http://192.168.0.100:8080/api/snapshot.png"
```

```json
{
  "id": "tv-01",
  "nome": "TV SERRALHERIA",
  "ip": "192.168.0.100",
  "channel_id": "880042",
  "image_url": "http://192.168.0.100:8080/api/snapshot.png"
}
```

---

## TVs Roku (App Nativo)

Para instalação nova, instale o app em `kiosk-roku/` nas TVs Roku habilitando o **Developer Mode**. O `visualizador.html` gerado pelo sistema é a página que o app da kiosk abre na TV.

---

## Roadmap

- [ ] Módulo MQTT para dispositivos IoT
- [ ] Módulo ONVIF para câmeras/monitores
- [x] Dashboard web de gerenciamento (painel.html + rota `/api/roku/painel`)
- [ ] Alertas e notificações (webhook, e-mail)
- [ ] Histórico de eventos e logs
- [ ] Suporte a múltiplos protocols em paralelo

---

## Licença

GNU General Public License v3.0 — veja [LICENSE](LICENSE).
