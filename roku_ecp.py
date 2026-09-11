"""
roku_ecp.py - Modulo Roku ECP (External Control Protocol)

Primeiro modulo do sistema Visualizador de Dispositivos.
Suporte a multiplas TVs Roku com:
  - Cadastro via tvs_config.json (CRUD pela API)
  - Agendamento de ligar/desligar tela por TV
  - Watchdog por TV (garante o app na tela)
  - SSDP Discovery + Polling de borda offline->online
  - Power On/Off via ECP keypress

Endpoints FastAPI:
  GET    /api/roku/tvs                  - lista TVs
  POST   /api/roku/tvs                  - cadastra TV
  PUT    /api/roku/tvs/{id}             - edita TV
  DELETE /api/roku/tvs/{id}             - remove TV
  POST   /api/roku/tvs/{id}/launch      - lanca app agora
  POST   /api/roku/tvs/{id}/power-on   - liga a tela
  POST   /api/roku/tvs/{id}/power-off  - desliga a tela
  GET    /api/roku/tvs/{id}/status      - status da TV
  GET    /api/roku/tvs/{id}/apps        - apps instalados
"""

import asyncio
import json
import os
import re
import socket
import struct
import time
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("roku_ecp")

# ---------------------------------------------------------------------------
# Configuracoes
# ---------------------------------------------------------------------------
ROKU_ECP_PORT: int = 8060

_LAUNCH_COOLDOWN_SECONDS: int = 60
_RETRY_MAX_ATTEMPTS: int = 5
_RETRY_INTERVAL_SECONDS: int = 10
_POLL_INTERVAL_SECONDS: int = 5

_SSDP_MULTICAST_IP = "239.255.255.250"
_SSDP_PORT = 1900

# ---------------------------------------------------------------------------
# Banco de dados (tvs_config.json)
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).parent
_TVS_FILE = _BASE_DIR / "tvs_config.json"

_tv_state: Dict[str, dict] = {}


def _load_tvs() -> List[dict]:
    try:
        if _TVS_FILE.exists():
            return json.loads(_TVS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[ROKU] Erro ao carregar tvs_config.json: {e}")
    return []


def _save_tvs(tvs: List[dict]):
    try:
        _TVS_FILE.write_text(json.dumps(tvs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"[ROKU] Erro ao salvar tvs_config.json: {e}")


def _get_tv(tv_id: str) -> Optional[dict]:
    tvs = _load_tvs()
    return next((t for t in tvs if t["id"] == tv_id), None)


def _init_state(tv: dict):
    tid = tv["id"]
    if tid not in _tv_state:
        _tv_state[tid] = {
            "last_launch_time": 0.0,
            "launch_in_progress": False,
            "was_online": None,
            "watchdog_status": {
                "last_check": None,
                "active_app_id": None,
                "our_app_active": None,
                "tv_online": None,
            },
        }


# ---------------------------------------------------------------------------
# Funcoes ECP HTTP
# ---------------------------------------------------------------------------

async def _roku_is_online(ip: str) -> bool:
    """Verifica se a TV esta respondendo E com tela ligada (power-mode = PowerOn)."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{ip}:{ROKU_ECP_PORT}/query/device-info")
            if resp.status_code == 200:
                if "<power-mode>PowerOn</power-mode>" not in resp.text:
                    return False
                return True
    except Exception:
        pass
    return False


async def _roku_is_reachable(ip: str) -> bool:
    """Verifica se a TV responde na rede (independente do power-mode)."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{ip}:{ROKU_ECP_PORT}/query/device-info")
            return resp.status_code == 200
    except Exception:
        pass
    return False


async def _roku_device_info(ip: str) -> Optional[dict]:
    """Retorna informacoes basicas do dispositivo Roku."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"http://{ip}:{ROKU_ECP_PORT}/query/device-info")
            if resp.status_code == 200:
                text = resp.text
                def _x(tag):
                    m = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
                    return m.group(1).strip() if m else None
                return {
                    "model_name":       _x("model-name"),
                    "friendly_name":    _x("friendly-device-name"),
                    "software_version": _x("software-version"),
                    "serial_number":    _x("serial-number"),
                    "power_mode":       _x("power-mode"),
                }
    except Exception:
        pass
    return None


async def _roku_power(ip: str, action: str) -> bool:
    """Envia comando de Power via ECP keypress. action = 'PowerOn' ou 'PowerOff'."""
    url = f"http://{ip}:{ROKU_ECP_PORT}/keypress/{action}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url)
            if resp.status_code in (200, 204):
                logger.info(f"[ROKU ECP] Keypress {action} enviado para {ip} (HTTP {resp.status_code})")
                return True
            logger.warning(f"[ROKU ECP] Keypress {action} em {ip}: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"[ROKU ECP] Falha ao enviar {action} para {ip}: {e}")
        return False


async def _roku_launch(ip: str, channel_id: str) -> bool:
    """Envia POST ECP para lancar o app. Retorna True se sucesso."""
    url = f"http://{ip}:{ROKU_ECP_PORT}/launch/{channel_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url)
            if resp.status_code in (200, 204):
                logger.info(f"[ROKU ECP] App {channel_id} lancado em {ip} (HTTP {resp.status_code})")
                return True
            logger.warning(f"[ROKU ECP] Resposta inesperada ao lancar app: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"[ROKU ECP] Falha ao lancar app em {ip}: {e}")
        return False


async def _roku_query_apps(ip: str) -> Optional[list]:
    """Consulta lista de apps instalados. Retorna lista de dicts ou None."""
    url = f"http://{ip}:{ROKU_ECP_PORT}/query/apps"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                apps = []
                for m in re.finditer(r'<app id="([^"]+)"[^>]*>([^<]+)</app>', resp.text):
                    apps.append({"id": m.group(1), "name": m.group(2).strip()})
                return apps
    except Exception as e:
        logger.error(f"[ROKU ECP] Falha ao consultar apps: {e}")
    return None


async def _roku_active_app(ip: str) -> Optional[str]:
    """Consulta o app ativo no momento. Retorna o ID ou None."""
    url = f"http://{ip}:{ROKU_ECP_PORT}/query/active-app"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                m = re.search(r'<app[^>]+id="([^"]+)"', resp.text)
                return m.group(1) if m else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Launch com retry por TV
# ---------------------------------------------------------------------------

async def _launch_with_retry(tv: dict, source: str):
    tid = tv["id"]
    ip = tv["ip"]
    channel_id = tv.get("channel_id", "")
    state = _tv_state.get(tid, {})

    if state.get("launch_in_progress"):
        logger.debug(f"[ROKU ECP] [{tv['nome']}] Launch ja em andamento, ignorando.")
        return

    _tv_state[tid]["launch_in_progress"] = True
    try:
        logger.info(f"[ROKU ECP] [{tv['nome']}] Iniciando launch (fonte: {source})...")
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 2):
            online = await _roku_is_online(ip)
            if not online:
                logger.warning(f"[ROKU ECP] [{tv['nome']}] Tentativa {attempt}: TV nao responde. Aguardando {_RETRY_INTERVAL_SECONDS}s...")
                await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            success = await _roku_launch(ip, channel_id)
            if success:
                _tv_state[tid]["last_launch_time"] = time.time()
                logger.info(f"[ROKU ECP] [{tv['nome']}] App aberto na tentativa {attempt}.")
                return
            if attempt <= _RETRY_MAX_ATTEMPTS:
                logger.warning(f"[ROKU ECP] [{tv['nome']}] Tentativa {attempt} falhou. Retentando em {_RETRY_INTERVAL_SECONDS}s...")
                await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
        logger.error(f"[ROKU ECP] [{tv['nome']}] Todas as tentativas falharam.")
    finally:
        _tv_state[tid]["launch_in_progress"] = False


def _is_within_schedule(tv: dict) -> bool:
    """Retorna True se o horario atual estiver dentro do periodo de funcionamento da TV (entre schedule_on e schedule_off).
    Se nao houver schedule configurado, retorna True."""
    on_time = tv.get("schedule_on")
    off_time = tv.get("schedule_off")
    if not on_time or not off_time:
        return True
    
    try:
        now = datetime.now().time()
        t_on = datetime.strptime(on_time, "%H:%M").time()
        t_off = datetime.strptime(off_time, "%H:%M").time()
        
        if t_on < t_off:
            return t_on <= now <= t_off
        else: # cruza a meia noite
            return now >= t_on or now <= t_off
    except Exception:
        return True


async def _trigger_launch_if_ready(tv: dict, source: str = "auto"):
    if not tv.get("enabled"):
        return
        
    # Se o trigger for automatico (nao manual e nem o proprio scheduler ligando), 
    # respeita o horario programado de desligamento para evitar loop noturno.
    if source in ("SSDP", "polling", "watchdog"):
        if not _is_within_schedule(tv):
            # Nao faz log de warning para nao poluir a noite toda, apenas debug.
            logger.debug(f"[ROKU ECP] [{tv.get('nome')}] Fora do horario programado. Ignorando trigger '{source}'.")
            return

    channel_id = tv.get("channel_id", "")
    if not channel_id:
        logger.warning(f"[ROKU ECP] [{tv['nome']}] channel_id nao configurado.")
        return
    tid = tv["id"]
    state = _tv_state.get(tid, {})
    now_time = time.time()
    last = state.get("last_launch_time", 0.0)
    if last > 0 and (now_time - last) < _LAUNCH_COOLDOWN_SECONDS:
        remaining = int(_LAUNCH_COOLDOWN_SECONDS - (now_time - last))
        logger.debug(f"[ROKU ECP] [{tv['nome']}] Cooldown ativo ({remaining}s). Ignorando.")
        return
    asyncio.create_task(_launch_with_retry(tv, source))


# ---------------------------------------------------------------------------
# Agendador - verifica horarios e liga/desliga cada TV
# ---------------------------------------------------------------------------

async def _power_on_with_retry(tv: dict):
    """
    Tenta ligar a tela da TV com retry por ate 3 minutos (6 tentativas x 30s).

    IMPORTANTE: Para o PowerOn via rede funcionar, a TV precisa ter
    'Inicializacao Rapida da TV' (Fast TV Start) ativada:
    Configuracoes -> Sistema -> Energia -> Inicializacao rapida da TV

    Sem essa opcao, o Roku desliga o Wi-Fi junto com a tela e nao
    e possivel acorda-la remotamente.
    """
    ip   = tv["ip"]
    nome = tv["nome"]
    MAX_RETRIES    = 6   # 6 tentativas
    RETRY_INTERVAL = 30  # 30s entre cada tentativa = ate 3 minutos

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"[ROKU SCHEDULER] [{nome}] PowerOn tentativa {attempt}/{MAX_RETRIES}...")
        ok = await _roku_power(ip, "PowerOn")

        if ok:
            logger.info(f"[ROKU SCHEDULER] [{nome}] PowerOn aceito. Aguardando TV inicializar...")
            # Aguarda a TV realmente ficar com tela ligada (por ate 60s = 12x5s)
            for _ in range(12):
                await asyncio.sleep(5)
                if await _roku_is_online(ip):
                    logger.info(f"[ROKU SCHEDULER] [{nome}] TV online! Lancando app...")
                    current_tv = _get_tv(tv["id"]) or tv
                    _tv_state[tv["id"]]["last_launch_time"] = 0  # reseta cooldown
                    await _trigger_launch_if_ready(current_tv, source="scheduler-on")
                    return
            logger.warning(f"[ROKU SCHEDULER] [{nome}] TV nao ficou online apos PowerOn. Retentando em {RETRY_INTERVAL}s...")
        else:
            logger.warning(
                f"[ROKU SCHEDULER] [{nome}] PowerOn falhou (tentativa {attempt}). "
                f"Verifique se 'Inicializacao Rapida da TV' esta ativada no Roku. "
                f"Retentando em {RETRY_INTERVAL}s..."
            )

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_INTERVAL)

    logger.error(
        f"[ROKU SCHEDULER] [{nome}] Nao foi possivel ligar a TV apos {MAX_RETRIES} tentativas. "
        f"Ative 'Inicializacao Rapida da TV' em: Configuracoes -> Sistema -> Energia."
    )


async def _scheduler():
    """Verifica a hora atual e aplica schedule_on/schedule_off para cada TV cadastrada."""
    logger.info("[ROKU SCHEDULER] Agendador iniciado.")

    # Rastreia quais TVs ja receberam o comando neste minuto exato (evita envio duplo)
    _last_on_fired:  dict = {}
    _last_off_fired: dict = {}

    while True:
        now_str = datetime.now().strftime("%H:%M")
        tvs = _load_tvs()

        for tv in tvs:
            if not tv.get("enabled"):
                continue
            _init_state(tv)
            tid      = tv["id"]
            on_time  = tv.get("schedule_on",  "")
            off_time = tv.get("schedule_off", "")

            # Horario de LIGAR
            if on_time and now_str == on_time and _last_on_fired.get(tid) != now_str:
                _last_on_fired[tid] = now_str
                logger.info(f"[ROKU SCHEDULER] [{tv['nome']}] Horario de LIGAR ({on_time}). Iniciando power-on com retry...")
                asyncio.create_task(_power_on_with_retry(tv))

            # Horario de DESLIGAR
            if off_time and now_str == off_time and _last_off_fired.get(tid) != now_str:
                _last_off_fired[tid] = now_str
                logger.info(f"[ROKU SCHEDULER] [{tv['nome']}] Horario de DESLIGAR ({off_time}). Enviando PowerOff...")
                await _roku_power(tv["ip"], "PowerOff")

        await asyncio.sleep(30)  # checa a cada 30s para nao perder o minuto exato


# ---------------------------------------------------------------------------
# Polling por TV
# ---------------------------------------------------------------------------

async def _polling_watcher_tv(tv: dict):
    tid = tv["id"]
    ip  = tv["ip"]
    logger.info(f"[ROKU POLL] [{tv['nome']}] Watcher iniciado.")

    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    _was = await _roku_is_online(ip)
    _tv_state[tid]["was_online"] = _was

    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            current_tv = _get_tv(tid)
            if current_tv is None or not current_tv.get("enabled"):
                await asyncio.sleep(30)
                continue

            is_online = await _roku_is_online(ip)
            was = _tv_state[tid].get("was_online", False)

            if is_online and not was:
                logger.info(f"[ROKU POLL] [{tv['nome']}] Voltou ONLINE! Disparando launch...")
                await _trigger_launch_if_ready(current_tv, source="polling")
            if not is_online and was:
                logger.info(f"[ROKU POLL] [{tv['nome']}] Foi OFFLINE.")

            _tv_state[tid]["was_online"] = is_online
        except Exception as e:
            logger.debug(f"[ROKU POLL] [{tv['nome']}] Erro: {e}")


# ---------------------------------------------------------------------------
# Watchdog por TV
# ---------------------------------------------------------------------------

async def _app_watchdog_tv(tv: dict):
    tid = tv["id"]
    ip  = tv["ip"]
    interval = int(os.getenv("ROKU_WATCHDOG_INTERVAL", "30"))

    logger.info(f"[ROKU WATCHDOG] [{tv['nome']}] Guardiao iniciado (intervalo: {interval}s).")
    await asyncio.sleep(interval)

    while True:
        try:
            current_tv = _get_tv(tid)
            if current_tv is None or not current_tv.get("enabled") or not current_tv.get("watchdog", True):
                await asyncio.sleep(interval)
                continue

            online = await _roku_is_online(ip)
            if not online:
                _tv_state[tid]["watchdog_status"] = {
                    "last_check": time.time(), "active_app_id": None,
                    "our_app_active": None, "tv_online": False
                }
                await asyncio.sleep(interval)
                continue

            active_id = await _roku_active_app(ip)
            target_id = current_tv.get("channel_id", "")
            our_active = (active_id == target_id)

            _tv_state[tid]["watchdog_status"] = {
                "last_check": time.time(), "active_app_id": active_id,
                "our_app_active": our_active, "tv_online": True
            }

            if our_active:
                logger.debug(f"[ROKU WATCHDOG] [{tv['nome']}] App OK.")
            else:
                logger.warning(f"[ROKU WATCHDOG] [{tv['nome']}] App ativo: '{active_id}'. Relancando '{target_id}'...")
                await _trigger_launch_if_ready(current_tv, source="watchdog")

        except Exception as e:
            logger.debug(f"[ROKU WATCHDOG] [{tv['nome']}] Erro: {e}")

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# SSDP Listener global
# ---------------------------------------------------------------------------

def _recv_with_timeout(sock: socket.socket, bufsize: int, timeout: float):
    sock.settimeout(timeout)
    try:
        return sock.recvfrom(bufsize)
    except (socket.timeout, Exception):
        return None, None


async def _ssdp_listener():
    """Escuta anuncios SSDP e dispara launch para a TV que anunciar."""
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass
    sock.bind(("", _SSDP_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(_SSDP_MULTICAST_IP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    logger.info("[ROKU SSDP] Listener global ativo.")

    while True:
        try:
            data, addr = await loop.run_in_executor(None, lambda: _recv_with_timeout(sock, 2048, 2.0))
            if data is None:
                await asyncio.sleep(0.05)
                continue
            sender_ip = addr[0]
            msg = data.decode("utf-8", errors="ignore").lower()
            is_roku = "roku" in msg or "dial" in msg or "ssdp:alive" in msg
            if not is_roku:
                continue
            tvs = _load_tvs()
            for tv in tvs:
                if tv["ip"] == sender_ip and tv.get("enabled"):
                    _init_state(tv)
                    logger.info(f"[ROKU SSDP] Anuncio de {sender_ip} -> [{tv['nome']}]. Disparando launch...")
                    await _trigger_launch_if_ready(tv, source="SSDP")
        except Exception as e:
            if "timed out" not in str(e).lower():
                logger.debug(f"[ROKU SSDP] Erro: {e}")
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def start_roku_watcher():
    """Inicia todos os watchers. Chamado no lifespan do FastAPI."""
    tvs = _load_tvs()
    if not tvs:
        logger.info("[ROKU ECP] Nenhuma TV cadastrada em tvs_config.json.")

    logger.info(f"[ROKU ECP] Iniciando watchers para {len(tvs)} TV(s).")

    try:
        asyncio.create_task(_ssdp_listener())
    except Exception as e:
        logger.warning(f"[ROKU ECP] SSDP nao iniciou: {e}. Usando apenas polling.")

    asyncio.create_task(_scheduler())

    for tv in tvs:
        _init_state(tv)
        asyncio.create_task(_polling_watcher_tv(tv))
        if tv.get("watchdog", True):
            asyncio.create_task(_app_watchdog_tv(tv))

    logger.info("[ROKU ECP] Todos os watchers iniciados.")


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class TVCreate(BaseModel):
    nome: str
    ip: str
    channel_id: str = ""
    watchdog: bool = True
    schedule_on: str = ""
    schedule_off: str = ""
    enabled: bool = True


class TVUpdate(BaseModel):
    nome: Optional[str] = None
    ip: Optional[str] = None
    channel_id: Optional[str] = None
    watchdog: Optional[bool] = None
    schedule_on: Optional[str] = None
    schedule_off: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

roku_router = APIRouter(prefix="/api/roku", tags=["Roku ECP"])


@roku_router.get("/tvs")
async def api_list_tvs():
    """Lista todas as TVs cadastradas com status atual."""
    tvs = _load_tvs()
    result = []
    for tv in tvs:
        _init_state(tv)
        state = _tv_state.get(tv["id"], {})
        online = await _roku_is_online(tv["ip"])
        active_app = await _roku_active_app(tv["ip"]) if online else None
        info = await _roku_device_info(tv["ip"]) if online else None
        result.append({
            **tv,
            "tv_online": online,
            "active_app_id": active_app,
            "device_info": info,
            "launch_in_progress": state.get("launch_in_progress", False),
            "last_launch_ago_seconds": (
                int(time.time() - state["last_launch_time"])
                if state.get("last_launch_time", 0) > 0 else None
            ),
            "watchdog_status": state.get("watchdog_status", {}),
        })
    return result


@roku_router.post("/tvs", status_code=201)
async def api_add_tv(body: TVCreate):
    """Cadastra uma nova TV."""
    tvs = _load_tvs()
    new_tv = {"id": str(uuid.uuid4())[:8], **body.model_dump()}
    tvs.append(new_tv)
    _save_tvs(tvs)
    _init_state(new_tv)
    asyncio.create_task(_polling_watcher_tv(new_tv))
    if new_tv.get("watchdog"):
        asyncio.create_task(_app_watchdog_tv(new_tv))
    return new_tv


@roku_router.put("/tvs/{tv_id}")
async def api_update_tv(tv_id: str, body: TVUpdate):
    """Atualiza configuracoes de uma TV."""
    tvs = _load_tvs()
    idx = next((i for i, t in enumerate(tvs) if t["id"] == tv_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    tvs[idx].update(updates)
    _save_tvs(tvs)
    return tvs[idx]


@roku_router.delete("/tvs/{tv_id}")
async def api_delete_tv(tv_id: str):
    """Remove uma TV do cadastro."""
    tvs = _load_tvs()
    new_list = [t for t in tvs if t["id"] != tv_id]
    if len(new_list) == len(tvs):
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    _save_tvs(new_list)
    _tv_state.pop(tv_id, None)
    return {"ok": True, "message": "TV removida."}


@roku_router.post("/tvs/{tv_id}/launch")
async def api_launch(tv_id: str):
    """Lanca o app da TV manualmente (ignora cooldown)."""
    tv = _get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    _init_state(tv)
    _tv_state[tv_id]["last_launch_time"] = 0
    online = await _roku_is_online(tv["ip"])
    if not online:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": f"TV {tv['ip']} nao esta respondendo."
        })
    asyncio.create_task(_launch_with_retry(tv, source="manual/API"))
    return {"ok": True, "nome": tv["nome"], "ip": tv["ip"], "message": "Launch iniciado."}


@roku_router.post("/tvs/{tv_id}/power-on")
async def api_power_on(tv_id: str):
    """Liga a tela da TV via ECP PowerOn."""
    tv = _get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    ok = await _roku_power(tv["ip"], "PowerOn")
    return {"ok": ok, "nome": tv["nome"], "ip": tv["ip"], "action": "PowerOn"}


@roku_router.post("/tvs/{tv_id}/power-off")
async def api_power_off(tv_id: str):
    """Desliga a tela da TV via ECP PowerOff."""
    tv = _get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    ok = await _roku_power(tv["ip"], "PowerOff")
    return {"ok": ok, "nome": tv["nome"], "ip": tv["ip"], "action": "PowerOff"}


@roku_router.get("/tvs/{tv_id}/status")
async def api_tv_status(tv_id: str):
    """Status completo de uma TV."""
    tv = _get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    _init_state(tv)
    state = _tv_state.get(tv_id, {})
    online = await _roku_is_online(tv["ip"])
    active_app = await _roku_active_app(tv["ip"]) if online else None
    info = await _roku_device_info(tv["ip"]) if online else None
    return {
        **tv,
        "tv_online": online,
        "active_app_id": active_app,
        "device_info": info,
        "launch_in_progress": state.get("launch_in_progress", False),
        "last_launch_ago_seconds": (
            int(time.time() - state["last_launch_time"])
            if state.get("last_launch_time", 0) > 0 else None
        ),
        "watchdog_status": state.get("watchdog_status", {}),
    }


@roku_router.get("/tvs/{tv_id}/apps")
async def api_tv_apps(tv_id: str):
    """Lista apps instalados na TV."""
    tv = _get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    reachable = await _roku_is_reachable(tv["ip"])
    if not reachable:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": f"TV {tv['ip']} nao esta respondendo."
        })
    apps = await _roku_query_apps(tv["ip"])
    return {"ok": True, "nome": tv["nome"], "apps": apps or []}

@roku_router.get("/visualizador")
async def get_visualizador():
    """Retorna a página do visualizador de dispositivos."""
    from fastapi.responses import FileResponse
    path = _BASE_DIR / "visualizador.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Visualizador não encontrado no módulo.")
    return FileResponse(path)

