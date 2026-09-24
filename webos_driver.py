"""
webos_driver.py - Driver LG webOS (TvDriver) + Router /api/lg

Controle de TVs LG webOS pelo canal WebSocket padronizado (porta 3000,
TLS) da API "webOS TV Connect", a mesma usada por apps remotas e pelo
Home Assistant (via aiowebostv).

Diferencas importantes em relacao ao Roku ECP:

  - Nao ha "channel_id": o kiosk e aberto no NAVEGADOR NATIVO da LG
    (com.webos.app.browser) apontando para a URL de monitoramento do
    servidor. O campo "image_url" da TV (ou a var WEBOS_KIOSK_URL) define
    a URL; sem ela o launch falha.
  - Ligar (PowerOn) por rede usa WAKE-ON-LAN (campo "mac"); a TV precisa
    ter "Sempre pronto / Inicio rapido" ativo na rede.
  - Desligar / launch / apps / foreground usam comandos ssap-luna via WS.
  - Exige PAREAMENTO na primeira conexao: a TV mostra um dialogo para
    aceitar a conexao; a chave (client_key) fica salva em <modulo>/keys/
    (ou no caminho do campo "key_file"), reutilizada nas proximas vezes.

Dependencia opcional da biblioteca `aiowebostv`. Se nao estiver instalada
o modulo carrega (o router e registrado), mas as operacoes de rede falham
com log claro.
"""

import asyncio
import logging
import os
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import base
from .drivers import get_driver, register_driver

logger = logging.getLogger("lg_webos")


def _new_id() -> str:
    return str(uuid.uuid4())[:8]

# ---------------------------------------------------------------------------
# Dependencia aiowebostv (opcional - degrade gracioso se ausente)
# ---------------------------------------------------------------------------
try:
    from aiowebostv import WebOsClient
except Exception:  # pragma: no cover
    WebOsClient = None

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
_WEBOS_WS_PORT = 3000            # canal WebSocket TLS de controle
_WEBOS_HTTP_DISCOVERY_PORT = 3001  # porta de descoberta (webOS 3.0+)
BROWSER_APP_ID = "com.webos.app.browser"

_KEYS_DIR = Path(__file__).parent / "keys"

_clients: Dict[str, Any] = {}   # ip -> WebOsClient (conexao persistente)


# ---------------------------------------------------------------------------
# Wake-on-LAN (pacote magico) - stdlib, sem dependencia
# ---------------------------------------------------------------------------

def build_wol_packet(mac: str) -> Optional[bytes]:
    """Monta o magic packet WoL a partir de um MAC em qualquer formato comum.

    Aceita "AA:BB:CC:DD:EE:FF", "AA-BB-...", "AABB.CCDD.EEFF", "AABBCCDDEEFF".
    """
    raw = re.sub(r"[:.\-_ ]", "", mac.strip())
    if len(raw) != 12 or not all(c in "0123456789abcdefABCDEF" for c in raw):
        return None
    mac_bytes = bytes.fromhex(raw)
    return b"\xff" * 6 + mac_bytes * 16


def _send_wol(ip: str, mac: str, ports=(9, 7)) -> bool:
    packet = build_wol_packet(mac)
    if packet is None:
        return False
    sent = False
    for addr in (ip, "255.255.255.255"):
        for port in ports:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.settimeout(1.0)
                    s.sendto(packet, (addr, port))
                sent = True
            except OSError:
                continue
    return sent


# ---------------------------------------------------------------------------
# Persistencia da chave de pareamento (client_key), por TV
# ---------------------------------------------------------------------------

def _key_path(tv: dict) -> Path:
    cfg = (tv.get("key_file") or "").strip()
    if cfg:
        p = Path(cfg)
        return p if p.is_absolute() else Path(__file__).parent / p
    return _KEYS_DIR / f"{tv['id']}.key"


def _read_key(tv: dict) -> Optional[str]:
    try:
        k = _key_path(tv).read_text(encoding="utf-8").strip()
        return k or None
    except OSError:
        return None


def _write_key(tv: dict, key: Optional[str]):
    if not key:
        return
    try:
        path = _key_path(tv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
    except OSError as e:
        logger.warning(f"[LG] Nao foi possivel salvar a key de pareamento: {e}")


# ---------------------------------------------------------------------------
# URL do kiosk (pagina de monitoramento que a LG abre no navegador)
# ---------------------------------------------------------------------------

# URL padrão exibida no iframe quando a TV não tem image_url configurada.
# Pode ser sobrescrita pela variável de ambiente WEBOS_KIOSK_URL ou pelo
# campo image_url de cada TV no tvs_config.json.
_DEFAULT_KIOSK_CONTENT_URL = os.getenv(
    "WEBOS_KIOSK_URL",
    "https://seibtcomercial.github.io/VELOCIMETRO_SEIBT_-/",
)


def _detect_server_ip() -> str:
    """Descobre o IP LAN deste servidor (sem enviar trafego, socket UDP)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def resolve_kiosk_url(tv: dict) -> str:
    """Monta a URL que a TV LG vai abrir no navegador.

    Arquitetura iframe:
      visualizador.html  (wrapper: wake-lock, fullscreen, anti-sleep)
        └─ iframe src=?url=<content_url>

    Resolução do content_url (URL exibida DENTRO do iframe):
      1. campo ``image_url`` da TV no tvs_config.json
      2. variável de ambiente WEBOS_KIOSK_URL
      3. URL padrão (velocímetro GitHub Pages)

    A URL retornada sempre aponta para o wrapper local ``/api/roku/visualizador``
    com o content_url como parâmetro ?url=. Isso garante que wake-lock e
    fullscreen funcionem em todas as TVs, independente do conteúdo exibido.
    Para adicionar um novo painel numa TV basta configurar image_url no
    cadastro dela.
    """
    import urllib.parse

    # 1. image_url da TV → 2. env WEBOS_KIOSK_URL → 3. default
    content_url = (
        (tv.get("image_url") or "").strip().rstrip("/")
        or _DEFAULT_KIOSK_CONTENT_URL.strip().rstrip("/")
    )

    # Descobre a URL base do servidor para montar o wrapper
    env_server = os.getenv("WEBOS_SERVER_URL", "").strip().rstrip("/")
    if env_server:
        base = env_server
    else:
        ip = _detect_server_ip()
        port = os.getenv("WEBOS_SERVER_PORT", "8080").strip() or "8080"
        base = f"http://{ip}:{port}" if ip else ""

    if not base:
        # Sem IP detectado: retorna o content_url diretamente (degradação)
        return content_url

    import time
    cb = int(time.time())
    return f"{base}/api/roku/visualizador?_t={cb}&url={urllib.parse.quote(content_url, safe='')}"


# ---------------------------------------------------------------------------
# Gerenciamento da conexao persistente (por IP)
# ---------------------------------------------------------------------------

def _find_tv(ip: Optional[str] = None, tid: Optional[str] = None) -> Optional[dict]:
    for t in base.load_tvs():
        if (tid is not None and t.get("id") == tid) or (ip is not None and t.get("ip") == ip):
            return t
    return None


async def _dispose_client(client):
    try:
        await client.disconnect()
    except Exception:
        pass
    try:
        await client.close_client_session()
    except Exception:
        pass


async def _get_client(ip: str) -> Any:
    """Retorna (re)conectando o WebOsClient da TV. None se inalcancavel/nopar."""
    if WebOsClient is None:
        logger.error("[LG] aiowebostv nao instalado. Execute: pip install aiowebostv")
        return None
    tv = _find_tv(ip=ip)
    if tv is None:
        return None

    client = _clients.get(ip)
    if client is not None:
        try:
            if client.is_connected():
                return client
        except Exception:
            pass
        try:
            if await asyncio.wait_for(client.connect(), timeout=10):
                return client
        except Exception:
            pass
        await _dispose_client(client)
        _clients.pop(ip, None)

    client = WebOsClient(ip, client_key=_read_key(tv), connect_timeout=3.0)
    try:
        ok = await asyncio.wait_for(client.connect(), timeout=12)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"[LG] Falha ao conectar em {ip}: {e}")
        await _dispose_client(client)
        return None
    if not ok:
        logger.debug(f"[LG] Conexao recusada/sem pareamento em {ip}.")
        await _dispose_client(client)
        return None
    if client.client_key:
        _write_key(tv, client.client_key)
    _clients[ip] = client
    return client


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class LgWebOSDriver:
    brand = "webos"

    async def is_reachable(self, ip: str) -> bool:
        for port in (_WEBOS_WS_PORT, _WEBOS_HTTP_DISCOVERY_PORT):
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=2.0)
                w.close()
                return True
            except Exception:
                continue
        return False

    async def is_online(self, ip: str) -> bool:
        if not await self.is_reachable(ip):
            return False
        client = await _get_client(ip)
        if client is None:
            # Sem pareamento ainda: porta aberta conta como ligada (otimista),
            # assim o launch acontece e dispara o dialogo de pareamento.
            return True
        try:
            st = client.tv_state
            if st.is_screen_on or st.is_on:
                return True
            # Estado de energia ainda nao recebido -> nao sabemos, assume ligada.
            return not bool(st.power_state)
        except Exception:
            return True

    async def device_info(self, ip: str) -> Optional[dict]:
        client = await _get_client(ip)
        if client is None:
            return None
        try:
            di = client.tv_info
            hello = di.hello or {}
            hw = di.system or {}
            sw = di.software or {}
            return {
                "model_name": hw.get("modelName") or hello.get("modelName"),
                "friendly_name": hello.get("deviceName"),
                "sdk_version": hello.get("sdkVersion"),
                "software_version": (
                    sw.get("majorVer")
                    or (sw.get("minorVer") and f"{sw.get('majorVer')}.{sw.get('minorVer')}")
                    or None
                ),
                "serial_number": hw.get("modelSerialNumber"),
                "uuid": hello.get("deviceUUID"),
            }
        except Exception as e:
            logger.debug(f"[LG] device_info falhou: {e}")
            return None

    async def set_power(self, tv: dict, on: bool) -> bool:
        mac = (tv.get("mac") or "").strip()
        if on:
            if mac:
                if _send_wol(tv["ip"], mac):
                    logger.info(f"[LG] [{tv['nome']}] WoL enviado para {tv['ip']} (MAC {mac}).")
                    return True
                logger.warning(f"[LG] [{tv['nome']}] MAC invalido: {mac!r}")
            client = await _get_client(tv["ip"])
            if client is not None:
                try:
                    await client.power_on()
                    logger.info(f"[LG] [{tv['nome']}] power_on enviado via WebSocket.")
                    return True
                except Exception as e:
                    logger.debug(f"[LG] power_on via WS falhou: {e}")
            return False

        # desligar
        client = await _get_client(tv["ip"])
        if client is None:
            return False
        try:
            await client.power_off()
            logger.info(f"[LG] [{tv['nome']}] PowerOff enviado.")
            return True
        except Exception as e:
            logger.warning(f"[LG] [{tv['nome']}] Falha ao desligar: {e}")
            return False

    def target_app_id(self, tv: dict) -> str:
        return (tv.get("kiosk_app_id") or "").strip() or BROWSER_APP_ID

    async def launch(self, tv: dict) -> bool:
        url = resolve_kiosk_url(tv)
        app_id = self.target_app_id(tv)
        if app_id != BROWSER_APP_ID and not url:
            url = ""
        if not url and app_id == BROWSER_APP_ID:
            logger.error(
                f"[LG] [{tv['nome']}] URL do kiosk nao definida. "
                f"Configure 'image_url' na TV ou a variavel WEBOS_KIOSK_URL."
            )
            return False
        client = await _get_client(tv["ip"])
        if client is None:
            return False
        try:
            if url:
                res = await client.launch_app_with_params(app_id, {"target": url})
                if (res or {}).get("returnValue") is False:
                    res = await client.launch_app_with_params(app_id, {"url": url})
                if (res or {}).get("returnValue") is False:
                    logger.warning(f"[LG] [{tv['nome']}] Launch de {app_id} rejeitado: {res}")
                    return False
            else:
                res = await client.launch_app_with_params(app_id, {})
                if (res or {}).get("returnValue") is False:
                    logger.warning(f"[LG] [{tv['nome']}] Launch de {app_id} rejeitado: {res}")
                    return False
            logger.info(f"[LG] [{tv['nome']}] App {app_id} aberto (kiosk: {url or 'nativo'}).")
            return True
        except Exception as e:
            logger.warning(f"[LG] [{tv['nome']}] Falha ao abrir {app_id}: {e}")
            return False

    async def query_apps(self, ip: str) -> Optional[list]:
        client = await _get_client(ip)
        if client is None:
            return None
        try:
            res = await asyncio.wait_for(client.get_apps(), timeout=8)
        except Exception as e:
            logger.warning(f"[LG] Falha ao listar apps: {e}")
            return None
        launch_points = (res or {}).get("launchPoints") or []
        return [
            {"id": a.get("id"), "name": a.get("title") or a.get("name", "")}
            for a in launch_points if a.get("id")
        ]

    async def active_app(self, ip: str) -> Optional[str]:
        client = await _get_client(ip)
        if client is None:
            return None
        try:
            res = await asyncio.wait_for(client.get_current_app(), timeout=5)
            if isinstance(res, dict):
                app_id = res.get("appId") or (res.get("payload") or {}).get("appId")
                if app_id:
                    return app_id
        except Exception:
            pass
        return getattr(client.tv_state, "current_app_id", None) or None

    def watchdog_target_app(self, tv: dict) -> Optional[str]:
        return self.target_app_id(tv)

    def match_ssdp(self, message: str) -> bool:
        return (
            "webos" in message
            or "webos-second-screen" in message
            or "smartshare" in message
            or "lge-com" in message
            or "lgelectronics" in message
        )


register_driver(LgWebOSDriver())


# ---------------------------------------------------------------------------
# Router FastAPI /api/lg
# ---------------------------------------------------------------------------

lg_router = APIRouter(prefix="/api/lg", tags=["LG webOS"])

_lg_engine: Optional[base.DeviceEngine] = None


def _lg_tv_or_404(tv_id: str) -> dict:
    tv = base.get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    if base.tv_tipo(tv) != "webos":
        raise HTTPException(status_code=404, detail="TV nao encontrada (marca diferente)")
    return tv


def _engine() -> base.DeviceEngine:
    return _lg_engine or base.DeviceEngine(brands={"webos"}, log_tag="LG")


async def _safe(coro):
    try:
        return await asyncio.wait_for(coro, timeout=6)
    except Exception:
        return None


async def _status_payload(tv: dict) -> dict:
    base.init_state(tv)
    state = base._tv_state.get(tv["id"], {})
    driver = get_driver("webos")
    online = await _safe(driver.is_online(tv["ip"]))
    active_app = await _safe(driver.active_app(tv["ip"])) if online else None
    info = await _safe(driver.device_info(tv["ip"])) if online else None
    last = state.get("last_launch_time", 0)
    return {
        **tv,
        "tv_online": bool(online),
        "active_app_id": active_app,
        "device_info": info,
        "launch_in_progress": state.get("launch_in_progress", False),
        "last_launch_ago_seconds": int(time.time() - last) if last > 0 else None,
        "watchdog_status": state.get("watchdog_status", {}),
    }


@lg_router.get("/tvs")
async def api_lg_list_tvs():
    """Lista as TVs LG cadastradas com status atual."""
    return [await _status_payload(t) for t in base.load_tvs() if base.tv_tipo(t) == "webos"]


@lg_router.post("/tvs", status_code=201)
async def api_lg_add_tv(body: base.TVCreate):
    """Cadastra uma TV (sempre do tipo webos neste endpoint)."""
    tvs = base.load_tvs()
    data = body.model_dump()
    data["tipo"] = "webos"
    new_tv = {"id": str(_new_id()), **data}
    tvs.append(new_tv)
    base.save_tvs(tvs)
    base.init_state(new_tv)
    return new_tv


@lg_router.put("/tvs/{tv_id}")
async def api_lg_update_tv(tv_id: str, body: base.TVUpdate):
    """Atualiza configuracoes de uma TV LG."""
    tvs = base.load_tvs()
    idx = next((i for i, t in enumerate(tvs) if t["id"] == tv_id), None)
    if idx is None or base.tv_tipo(tvs[idx]) != "webos":
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    updates.pop("tipo", None)
    if updates.get("ip") and updates["ip"] != tvs[idx]["ip"]:
        await dispose_client(tvs[idx]["ip"])
    tvs[idx].update(updates)
    base.save_tvs(tvs)
    return tvs[idx]


@lg_router.delete("/tvs/{tv_id}")
async def api_lg_delete_tv(tv_id: str):
    """Remove uma TV LG do cadastro."""
    tvs = base.load_tvs()
    tv = next((t for t in tvs if t["id"] == tv_id), None)
    if tv is None or base.tv_tipo(tv) != "webos":
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    new_list = [t for t in tvs if t["id"] != tv_id]
    base.save_tvs(new_list)
    base._tv_state.pop(tv_id, None)
    await dispose_client(tv["ip"])
    return {"ok": True, "message": "TV removida."}


async def dispose_client(ip: str):
    """Fecha a conexao persistente de uma TV (usado ao remover/editar)."""
    client = _clients.pop(ip, None)
    if client is not None:
        await _dispose_client(client)


async def pair(tv: dict) -> dict:
    """Pareia a TV pelo fluxo aiowebostv (perguntar ao usuario na TV).

    Pode demorar: a TV exibe 'Conexao solicitada' e pode pedir um numero
    verificado no display. A chave gerada fica salva e reutilizada depois.
    """
    try:
        client = await asyncio.wait_for(_get_client(tv["ip"]), timeout=30)
    except asyncio.TimeoutError:
        client = None
        timed_out = True
    else:
        timed_out = False
    if client is None:
        if timed_out:
            detail = (
                "Tempo esgotado. Na TV, confirme a solicitacao de conexao "
                "(e confira se ela aparece a primeira vez que o servidor conecta)."
            )
        else:
            detail = "Nao foi possivel conectar. Verifique IP e acesso remoto da TV."
        return {"ok": False, "error": detail, "_code": 503}
    registered = bool(client.is_registered())
    if registered and client.client_key:
        _write_key(tv, client.client_key)
    return {
        "ok": True,
        "registered": registered,
        "nome": tv["nome"],
        "ip": tv["ip"],
        "key_file": str(_key_path(tv)),
    }


@lg_router.post("/tvs/{tv_id}/pair")
async def api_lg_pair(tv_id: str):
    """Pareia a TV (first conexao exige aceitar o dialogo na tela da TV)."""
    tv = _lg_tv_or_404(tv_id)
    result = await pair(tv)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("_code", 503), content=result)
    return result


@lg_router.post("/tvs/{tv_id}/launch")
async def api_lg_launch(tv_id: str):
    """Abre o navegador da TV na URL do kiosk (ignora cooldown)."""
    tv = _lg_tv_or_404(tv_id)
    base.init_state(tv)
    base._tv_state[tv_id]["last_launch_time"] = 0
    asyncio.create_task(_engine().launch_with_retry(tv, source="manual/API"))
    return {"ok": True, "nome": tv["nome"], "ip": tv["ip"], "message": "Launch iniciado."}


@lg_router.post("/tvs/{tv_id}/power-on")
async def api_lg_power_on(tv_id: str):
    """Liga a TV (Wake-on-LAN pelo 'mac' ou power_on via WebSocket)."""
    tv = _lg_tv_or_404(tv_id)
    ok = await get_driver("webos").set_power(tv, True)
    return {"ok": bool(ok), "nome": tv["nome"], "ip": tv["ip"], "action": "PowerOn"}


@lg_router.post("/tvs/{tv_id}/power-off")
async def api_lg_power_off(tv_id: str):
    """Desliga a TV (ssap://system/turnOff)."""
    tv = _lg_tv_or_404(tv_id)
    ok = await get_driver("webos").set_power(tv, False)
    return {"ok": bool(ok), "nome": tv["nome"], "ip": tv["ip"], "action": "PowerOff"}


@lg_router.get("/tvs/{tv_id}/status")
async def api_lg_tv_status(tv_id: str):
    """Status completo de uma TV LG."""
    return await _status_payload(_lg_tv_or_404(tv_id))


@lg_router.get("/tvs/{tv_id}/apps")
async def api_lg_tv_apps(tv_id: str):
    """Lista os apps instalados na TV LG."""
    tv = _lg_tv_or_404(tv_id)
    apps = await _safe(get_driver("webos").query_apps(tv["ip"]))
    if apps is None:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": f"TV {tv['ip']} nao respondeu / sem pareamento."
        })
    return {"ok": True, "nome": tv["nome"], "apps": apps}


@lg_router.get("/visualizador")
async def api_lg_visualizador():
    """Wrapper kiosk para abrir na TV LG.

    Mesmo visualizador.html do endpoint Roku. O wrapper:
      - Lê ?url= da query string (URL do painel a exibir)
      - Mantém tela acesa (Wake Lock + fallback canvas/video)
      - Solicita fullscreen automaticamente
      - Bloqueia saída acidental do kiosk (teclas Home/Exit)

    O backend (resolve_kiosk_url) monta a URL com ?url=<image_url da TV>
    automaticamente ao lançar o app webOS.
    """
    path = Path(__file__).parent / "visualizador.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Visualizador nao encontrado no modulo.")
    return FileResponse(path)



# ---------------------------------------------------------------------------
# Entry point (lifespan do FastAPI)
# ---------------------------------------------------------------------------

async def start_lg_watcher():
    """Inicia SSDP + scheduler + polling + watchdog para as TVs LG."""
    global _lg_engine
    _lg_engine = base.DeviceEngine(
        brands={"webos"},
        log_tag="LG",
        power_on_hint=(
            "Verifique o campo 'mac' (Wake-on-LAN) e ative 'Sempre pronto' "
            "na TV (Config. -> Sistema -> Energia)."
        ),
    )
    await _lg_engine.start()