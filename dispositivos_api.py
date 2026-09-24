"""
dispositivos_api.py - API universal /api/dispositivos

Painel unico: gerencia TVs de qualquer fabricante (roku, webos) por um
mesmo endpoint, despachando para o driver registrado (drivers.py).

O roteador de cada marca (/api/roku, /api/lg) continua funcionando e e
usado internamente pelo watchdog/polling; este router e so uma visao
unificada + CRUD para o painel web.
"""

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import base
from . import roku_ecp
from . import webos_driver
from .drivers import get_driver, known_types

logger = logging.getLogger("dispositivos")

router = APIRouter(prefix="/api/dispositivos", tags=["Dispositivos (universal)"])


def _find_tv(tv_id: str) -> dict:
    tv = base.get_tv(tv_id)
    if not tv:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    return tv


async def _safe(coro):
    try:
        return await asyncio.wait_for(coro, timeout=6)
    except Exception:
        return None


async def _status(tv: dict) -> dict:
    """Payload padronizado para qualquer marca (mesmo formato do Roku)."""
    base.init_state(tv)
    state = base._tv_state.get(tv["id"], {})
    driver = get_driver(base.tv_tipo(tv))
    online = False
    active_app = None
    info = None
    if driver is not None:
        online = bool(await _safe(driver.is_online(tv["ip"])))
        if online:
            active_app = await _safe(driver.active_app(tv["ip"]))
            info = await _safe(driver.device_info(tv["ip"]))
    last = state.get("last_launch_time", 0)
    return {
        **tv,
        "tipo": base.tv_tipo(tv),
        "tv_online": online,
        "active_app_id": active_app,
        "device_info": info,
        "launch_in_progress": state.get("launch_in_progress", False),
        "last_launch_ago_seconds": int(time.time() - last) if last > 0 else None,
        "watchdog_status": state.get("watchdog_status", {}),
    }


async def _start_per_tv_watchers(tv: dict):
    """Dispara os watchers de background equivalentes ao cadastro nativo."""
    if base.tv_tipo(tv) != "roku":
        return
    try:
        asyncio.create_task(roku_ecp._polling_watcher_tv(tv))
        if tv.get("watchdog"):
            asyncio.create_task(roku_ecp._app_watchdog_tv(tv))
    except Exception as e:
        logger.warning(f"[univ] Falha ao iniciar watchers de {tv['id']}: {e}")


# ---------------------------------------------------------------------------
# CRUD universal
# ---------------------------------------------------------------------------

@router.get("/discover")
async def api_discover():
    """Varre a rede (SSDP + TCP) e lista APENAS TVs novas (nao cadastradas).

    Trava: TVs ja cadastradas ficam escondidas da deteccao. Se uma TV for
    excluida do cadastro, volta a aparecer aqui na proxima varredura.
    """
    found = await base.discover_tvs()
    return [d for d in found if not d.get("configured")]


@router.get("")
async def api_list_all():
    """Lista todas as TVs (qualquer marca) com status atual."""
    return [await _status(t) for t in base.load_tvs()]


@router.post("", status_code=201)
async def api_add_tv(body: base.TVCreate):
    """Cadastra uma TV (campo tipo: roku | webos)."""
    tvs = base.load_tvs()
    data = body.model_dump()
    tipo = (data.get("tipo") or "roku").strip().lower()
    if tipo not in known_types():
        tipo = "roku"
    data["tipo"] = tipo
    new_tv = {"id": str(uuid.uuid4())[:8], **data}
    tvs.append(new_tv)
    base.save_tvs(tvs)
    base.init_state(new_tv)
    await _start_per_tv_watchers(new_tv)
    return new_tv


@router.put("/{tv_id}")
async def api_update_tv(tv_id: str, body: base.TVUpdate):
    """Atualiza configuracoes de qualquer TV."""
    tvs = base.load_tvs()
    idx = next((i for i, t in enumerate(tvs) if t["id"] == tv_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates.get("tipo") is not None:
        updates["tipo"] = (updates["tipo"] or "roku").strip().lower() or "roku"
        if updates["tipo"] not in known_types():
            updates["tipo"] = "roku"
    if base.tv_tipo(tvs[idx]) == "webos" and updates.get("ip"):
        await webos_driver.dispose_client(tvs[idx]["ip"])
    tvs[idx].update(updates)
    base.save_tvs(tvs)
    return tvs[idx]


@router.delete("/{tv_id}")
async def api_delete_tv(tv_id: str):
    """Remove uma TV qualquer do cadastro."""
    tvs = base.load_tvs()
    tv = next((t for t in tvs if t["id"] == tv_id), None)
    if tv is None:
        raise HTTPException(status_code=404, detail="TV nao encontrada")
    new_list = [t for t in tvs if t["id"] != tv_id]
    base.save_tvs(new_list)
    base._tv_state.pop(tv_id, None)
    if base.tv_tipo(tv) == "webos":
        await webos_driver.dispose_client(tv["ip"])
    return {"ok": True, "message": "TV removida."}


# ---------------------------------------------------------------------------
# Acoes
# ---------------------------------------------------------------------------

@router.post("/{tv_id}/launch")
async def api_launch(tv_id: str):
    """Lanca o app/kiosk da TV (ignora cooldown)."""
    tv = _find_tv(tv_id)
    base.init_state(tv)
    base._tv_state[tv_id]["last_launch_time"] = 0
    if base.tv_tipo(tv) == "roku":
        online = await _safe(roku_ecp._roku_is_online(tv["ip"]))
        if not online:
            return JSONResponse(status_code=503, content={
                "ok": False, "error": f"TV {tv['ip']} nao esta respondendo."
            })
        asyncio.create_task(roku_ecp._launch_with_retry(tv, source="manual/API"))
    else:
        asyncio.create_task(webos_driver._engine().launch_with_retry(tv, source="manual/API"))
    return {"ok": True, "nome": tv["nome"], "ip": tv["ip"], "message": "Launch iniciado."}


@router.post("/{tv_id}/power-on")
async def api_power_on(tv_id: str):
    tv = _find_tv(tv_id)
    ok = await get_driver(base.tv_tipo(tv)).set_power(tv, True)
    return {"ok": bool(ok), "nome": tv["nome"], "ip": tv["ip"], "action": "PowerOn"}


@router.post("/{tv_id}/power-off")
async def api_power_off(tv_id: str):
    tv = _find_tv(tv_id)
    ok = await get_driver(base.tv_tipo(tv)).set_power(tv, False)
    return {"ok": bool(ok), "nome": tv["nome"], "ip": tv["ip"], "action": "PowerOff"}


@router.get("/{tv_id}/status")
async def api_tv_status(tv_id: str):
    """Status completo de uma TV."""
    return await _status(_find_tv(tv_id))


@router.get("/{tv_id}/apps")
async def api_tv_apps(tv_id: str):
    """Lista os apps instalados na TV."""
    tv = _find_tv(tv_id)
    apps = await _safe(get_driver(base.tv_tipo(tv)).query_apps(tv["ip"]))
    if apps is None:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": f"TV {tv['ip']} nao respondeu."
        })
    return {"ok": True, "nome": tv["nome"], "apps": apps}


@router.post("/{tv_id}/pair")
async def api_pair(tv_id: str):
    """Pareia a TV (apenas LG webOS; Roku ja e open)."""
    tv = _find_tv(tv_id)
    if base.tv_tipo(tv) != "webos":
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Pareamento e exclusivo de TVs LG webOS."
        })
    result = await webos_driver.pair(tv)
    if not result.get("ok"):
        return JSONResponse(status_code=result.get("_code", 503), content=result)
    return result