"""
base.py - Nucleo compartilhado do Visualizador de Dispositivos.

Este modulo concentra tudo que NAO depende de marca/protocolo:

  - Persistencia no tvs_config.json (carga/salva/consulta + estado em memoria)
  - Agenda por dia da semana (schedules) e janelas de funcionamento
  - Agendador (l/d no horario programado)
  - Polling de borda offline -> online
  - Watchdog de app ativo
  - Listener SSDP global
  - Launch com retry e cooldown
  - Models Pydantic do cadastro de TVs (com o campo "tipo")

As diferencas entre fabricantes ficam nos drivers (ver drivers.py). Aqui o
nucleo apenas chama a interface TvDriver.

Comportamento de compatibilidade: um cadastro legado (sem campo "tipo") e
tratado como "roku", que e o tipo padrao.
"""

import asyncio
import json
import logging
import re
import socket
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import urlparse

from pydantic import BaseModel

from .drivers import get_driver

logger = logging.getLogger("visualizador")

# ---------------------------------------------------------------------------
# Configuracoes
# ---------------------------------------------------------------------------
_LAUNCH_COOLDOWN_SECONDS: int = 60
_RETRY_MAX_ATTEMPTS: int = 5
_RETRY_INTERVAL_SECONDS: int = 10
_POLL_INTERVAL_SECONDS: int = 5

_SSDP_MULTICAST_IP = "239.255.255.250"
_SSDP_PORT = 1900

_DEFAULT_TIPO = "roku"


# ---------------------------------------------------------------------------
# Banco de dados (tvs_config.json) + estado em memoria
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).parent
_TVS_FILE = _BASE_DIR / "tvs_config.json"

_tv_state: Dict[str, dict] = {}


def tv_tipo(tv: dict) -> str:
    """Tipo da TV. Cadastro legado (sem campo) e considerado 'roku'."""
    return tv.get("tipo") or _DEFAULT_TIPO


def load_tvs() -> List[dict]:
    try:
        if _TVS_FILE.exists():
            import json
            return json.loads(_TVS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[TV] Erro ao carregar tvs_config.json: {e}")
    return []


def save_tvs(tvs: List[dict]):
    try:
        import json
        _TVS_FILE.write_text(json.dumps(tvs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"[TV] Erro ao salvar tvs_config.json: {e}")


def get_tv(tv_id: str) -> Optional[dict]:
    return next((t for t in load_tvs() if t["id"] == tv_id), None)


def init_state(tv: dict):
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
# Agenda por dia da semana
# ---------------------------------------------------------------------------

# Dias da semana usados em "schedules" (mesmo padrao de datetime.weekday):
#   0=segunda, 1=terca, 2=quarta, 3=quinta, 4=sexta, 5=sabado, 6=domingo
# Nomes em portugues/ingles tambem sao aceitos no JSON.
_WEEKDAY_PT = {
    "segunda": 0, "segunda-feira": 0, "segundas": 0,
    "terca": 1, "terca-feira": 1, "tercas": 1,
    "quarta": 2, "quarta-feira": 2, "quartas": 2,
    "quinta": 3, "quinta-feira": 3, "quintas": 3,
    "sexta": 4, "sexta-feira": 4, "sextas": 4,
    "sabado": 5, "sábado": 5, "sabados": 5,
    "domingo": 6, "domingos": 6,
}
_WEEKDAY_EN = {
    "mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2,
    "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}


def _normalize_days(days) -> List[int]:
    """Converte a lista de dias de uma regra para numeros 0-6. Vazio = todos os dias."""
    if not days:
        return []
    result: List[int] = []
    seen = set()
    for d in days:
        if isinstance(d, bool):
            continue
        if isinstance(d, int):
            num = d % 7
        else:
            s = str(d).strip().lower()
            if s in _WEEKDAY_PT:
                num = _WEEKDAY_PT[s]
            elif s in _WEEKDAY_EN:
                num = _WEEKDAY_EN[s]
            else:
                continue
        if num not in seen:
            seen.add(num)
            result.append(num)
    return result


def _has_any_schedule(tv: dict) -> bool:
    """True se a TV possui qualquer configuracao de horario (nova ou legada)."""
    if tv.get("schedules"):
        return True
    return bool(tv.get("schedule_on") or tv.get("schedule_off"))


def _rule_active_today(rule: dict, today: int) -> bool:
    days = _normalize_days(rule.get("days"))
    if not days:
        return True  # dias vazios = todos os dias
    return today in days


def _today_schedule_rules(tv: dict) -> Optional[List[dict]]:
    """Retorna as regras de horario ativas hoje.

    - None             -> a TV nao tem agenda configurada (fica sempre ativa)
    - lista vazia      -> tem agenda, mas hoje nao ha regra (dia desligado)
    - lista com regras -> regras que valem para o dia atual (podem ser varias)
    """
    schedules = tv.get("schedules")
    if schedules:
        today = datetime.now().weekday()
        return [r for r in schedules if _rule_active_today(r, today)]
    on_time = tv.get("schedule_on", "")
    off_time = tv.get("schedule_off", "")
    if on_time or off_time:
        return [{"days": [], "on": on_time, "off": off_time}]
    return None


def _rule_in_interval(on_time: str, off_time: str, now) -> bool:
    try:
        t_on = datetime.strptime(on_time, "%H:%M").time()
        t_off = datetime.strptime(off_time, "%H:%M").time()
    except Exception:
        return False
    if t_on < t_off:
        return t_on <= now <= t_off
    return now >= t_on or now <= t_off  # cruza a meia noite


def _is_within_schedule(tv: dict) -> bool:
    """True se o horario atual estiver dentro do periodo de funcionamento da TV
    para o dia de hoje (considera agenda por dia da semana). Sem agenda, True."""
    if not _has_any_schedule(tv):
        return True
    rules = _today_schedule_rules(tv) or []
    if not rules:
        return False
    now = datetime.now().time()
    for rule in rules:
        if _rule_in_interval(rule.get("on", ""), rule.get("off", ""), now):
            return True
    return False


# ---------------------------------------------------------------------------
# Models Pydantic do cadastro (compartilhados por todas as marcas)
# ---------------------------------------------------------------------------

class ScheduleRule(BaseModel):
    days: List[Union[int, str]] = []
    on: str = ""
    off: str = ""


class TVCreate(BaseModel):
    tipo: str = "roku"
    nome: str
    ip: str
    channel_id: str = ""
    mac: str = ""
    key_file: str = ""
    kiosk_app_id: str = ""
    watchdog: bool = True
    schedule_on: str = ""
    schedule_off: str = ""
    schedules: List[ScheduleRule] = []
    image_url: str = ""
    enabled: bool = True


class TVUpdate(BaseModel):
    tipo: Optional[str] = None
    nome: Optional[str] = None
    ip: Optional[str] = None
    channel_id: Optional[str] = None
    mac: Optional[str] = None
    key_file: Optional[str] = None
    kiosk_app_id: Optional[str] = None
    watchdog: Optional[bool] = None
    schedule_on: Optional[str] = None
    schedule_off: Optional[str] = None
    schedules: Optional[List[ScheduleRule]] = None
    image_url: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Motor generico: launch com retry, agendador, polling, watchdog e SSDP
# (tudo parametrizado pelo driver; cada marca roda APENAS as TVs daquela marca)
# ---------------------------------------------------------------------------

class DeviceEngine:
    """Motor agnostico que roda as rotinas para um conjunto de marcas.

    brands=None roda para todas as marcas com driver registrado; brands={"webos"}
    roda somente para as TVs com tipo "webos" (util quando cada marca tem seu
    proprio ciclo no lifespan).
    """

    def __init__(self, brands: Optional[set] = None, log_tag: str = "TV",
                 power_on_hint: str = ""):
        self.brands: Optional[set] = set(brands) if brands else None
        self.log_tag = log_tag
        self.power_on_hint = power_on_hint

    # -- helpers ------------------------------------------------------------

    def _skip(self, tv: dict) -> bool:
        """True se a TV nao pertence a este motor (marca nao coberta)."""
        if self.brands is not None and tv_tipo(tv) not in self.brands:
            return True
        return False

    def _driver(self, tv: dict):
        """Driver da TV (respeitando o filtro de marcas do motor)."""
        if self._skip(tv):
            return None
        return get_driver(tv_tipo(tv))

    def _supports_launch(self, tv: dict) -> bool:
        """True se a TV precisa/alvo de launch de conteudo (Roku: channel_id)."""
        if tv_tipo(tv) == "roku":
            return bool(tv.get("channel_id", ""))
        # Demais marcas (ex.: webOS) abrem o navegador pela URL do kiosk;
        # image_url vazio usa o padrao do servidor, entao sempre permitido.
        return True

    # -- launch com retry ---------------------------------------------------

    async def launch_with_retry(self, tv: dict, source: str):
        driver = self._driver(tv)
        if driver is None:
            return

        tid = tv["id"]
        ip = tv["ip"]
        state = _tv_state.get(tid, {})

        if state.get("launch_in_progress"):
            logger.debug(f"[{self.log_tag}] [{tv['nome']}] Launch ja em andamento, ignorando.")
            return

        init_state(tv)
        _tv_state[tid]["launch_in_progress"] = True
        try:
            logger.info(f"[{self.log_tag}] [{tv['nome']}] Iniciando launch (fonte: {source})...")
            for attempt in range(1, _RETRY_MAX_ATTEMPTS + 2):
                online = await driver.is_online(ip)
                if not online:
                    logger.warning(
                        f"[{self.log_tag}] [{tv['nome']}] Tentativa {attempt}: "
                        f"TV nao responde. Aguardando {_RETRY_INTERVAL_SECONDS}s..."
                    )
                    await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
                    continue
                success = await driver.launch(tv)
                if success:
                    _tv_state[tid]["last_launch_time"] = time.time()
                    logger.info(f"[{self.log_tag}] [{tv['nome']}] App aberto na tentativa {attempt}.")
                    return
                if attempt <= _RETRY_MAX_ATTEMPTS:
                    logger.warning(
                        f"[{self.log_tag}] [{tv['nome']}] Tentativa {attempt} falhou. "
                        f"Retentando em {_RETRY_INTERVAL_SECONDS}s..."
                    )
                    await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
            logger.error(f"[{self.log_tag}] [{tv['nome']}] Todas as tentativas falharam.")
        finally:
            _tv_state[tid]["launch_in_progress"] = False

    async def trigger_launch_if_ready(self, tv: dict, source: str = "auto"):
        if not tv.get("enabled"):
            return

        # Trigger automatico (SSDP/polling/watchdog) respeita a janela de
        # funcionamento para evitar loop noturno.
        if source in ("SSDP", "polling", "watchdog"):
            if not _is_within_schedule(tv):
                logger.debug(
                    f"[{self.log_tag}] [{tv.get('nome')}] Fora do horario programado. "
                    f"Ignorando trigger '{source}'."
                )
                return

        if not self._supports_launch(tv):
            logger.warning(f"[{self.log_tag}] [{tv['nome']}] Conteudo kiosk nao configurado.")
            return

        tid = tv["id"]
        state = _tv_state.get(tid, {})
        now_time = time.time()
        last = state.get("last_launch_time", 0.0)
        if last > 0 and (now_time - last) < _LAUNCH_COOLDOWN_SECONDS:
            remaining = int(_LAUNCH_COOLDOWN_SECONDS - (now_time - last))
            logger.debug(f"[{self.log_tag}] [{tv['nome']}] Cooldown ativo ({remaining}s). Ignorando.")
            return
        asyncio.create_task(self.launch_with_retry(tv, source))

    # -- power-on com retry -------------------------------------------------

    async def power_on_with_retry(self, tv: dict, *, boot_wait_seconds: int = 60,
                                  hint: str = ""):
        """Liga a TV com retry, aguarda ficar online e dispara o launch."""
        driver = self._driver(tv)
        if driver is None:
            return

        ip = tv["ip"]
        nome = tv["nome"]
        MAX_RETRIES = 6
        RETRY_INTERVAL = 30
        hint_txt = f" ({hint})" if hint else ""

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"[{self.log_tag}] [{nome}] PowerOn tentativa {attempt}/{MAX_RETRIES}...")
            ok = await driver.set_power(tv, True)

            if ok:
                logger.info(f"[{self.log_tag}] [{nome}] PowerOn aceito. Aguardando TV inicializar...")
                max_checks = max(1, int(boot_wait_seconds // 5))
                for _ in range(max_checks):
                    await asyncio.sleep(5)
                    if await driver.is_online(ip):
                        logger.info(f"[{self.log_tag}] [{nome}] TV online! Aguardando boot final...")
                        await asyncio.sleep(30)
                        logger.info(f"[{self.log_tag}] [{nome}] Disparando launch pós-boot...")
                        current_tv = get_tv(tv["id"]) or tv
                        init_state(tv)
                        _tv_state[tv["id"]]["last_launch_time"] = 0  # reseta cooldown
                        await self.trigger_launch_if_ready(current_tv, source="scheduler-on")
                        return
                logger.warning(
                    f"[{self.log_tag}] [{nome}] TV nao ficou online apos PowerOn. "
                    f"Retentando em {RETRY_INTERVAL}s..."
                )
            else:
                logger.warning(
                    f"[{self.log_tag}] [{nome}] PowerOn falhou (tentativa {attempt})."
                    f"{hint_txt} Retentando em {RETRY_INTERVAL}s..."
                )

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_INTERVAL)

        logger.error(
            f"[{self.log_tag}] [{nome}] Nao foi possivel ligar a TV apos "
            f"{MAX_RETRIES} tentativas.{hint_txt}"
        )

    # -- agendador ----------------------------------------------------------

    async def scheduler(self):
        """Aplica a agenda de cada TV pertencente a este motor (l/d por dia)."""
        logger.info(f"[{self.log_tag} SCHEDULER] Agendador iniciado.")

        # Rastreia eventos ja disparados no dia (por TV + regra + evento).
        _fired_day: dict = {}

        while True:
            now = datetime.now()
            now_str = now.strftime("%H:%M")
            today_key = now.strftime("%Y-%m-%d")
            tvs = load_tvs()

            for tv in tvs:
                if not tv.get("enabled"):
                    continue
                driver = self._driver(tv)
                if driver is None:
                    continue
                init_state(tv)
                tid = tv["id"]

                if not _has_any_schedule(tv):
                    continue

                rules = _today_schedule_rules(tv) or []
                if not rules:
                    logger.debug(
                        f"[{self.log_tag} SCHEDULER] [{tv['nome']}] Hoje nao ha horario "
                        f"programado. TV fica desligada."
                    )
                    continue

                for rule_idx, rule in enumerate(rules):
                    on_time = rule.get("on", "")
                    off_time = rule.get("off", "")

                    # Horario de LIGAR
                    if on_time and now_str == on_time:
                        key = f"on|{tid}|{rule_idx}"
                        if _fired_day.get(key) != today_key:
                            _fired_day[key] = today_key
                            logger.info(
                                f"[{self.log_tag} SCHEDULER] [{tv['nome']}] Horario de LIGAR "
                                f"({on_time}). Iniciando power-on com retry..."
                            )
                            asyncio.create_task(self.power_on_with_retry(
                                tv, hint=self._power_on_hint()
                            ))

                    # Horario de DESLIGAR
                    if off_time and now_str == off_time:
                        key = f"off|{tid}|{rule_idx}"
                        if _fired_day.get(key) != today_key:
                            _fired_day[key] = today_key
                            logger.info(
                                f"[{self.log_tag} SCHEDULER] [{tv['nome']}] Horario de DESLIGAR "
                                f"({off_time}). Enviando PowerOff..."
                            )
                            await driver.set_power(tv, False)

            await asyncio.sleep(30)  # checa a cada 30s para nao perder o minuto exato

    def _power_on_hint(self) -> str:
        """Dica exibida quando o ligamento por rede falha (varia por marca)."""
        return self.power_on_hint

    # -- polling de borda offline -> online ---------------------------------

    async def polling_watcher_tv(self, tv: dict):
        driver = self._driver(tv)
        if driver is None:
            return

        tid = tv["id"]
        ip = tv["ip"]
        init_state(tv)
        logger.info(f"[{self.log_tag} POLL] [{tv['nome']}] Watcher iniciado.")

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        _was = await driver.is_online(ip)
        _tv_state[tid]["was_online"] = _was

        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            try:
                current_tv = get_tv(tid)
                if current_tv is None or not current_tv.get("enabled"):
                    await asyncio.sleep(30)
                    continue
                if self._skip(current_tv):
                    await asyncio.sleep(30)
                    continue

                is_online = await driver.is_online(ip)
                was = _tv_state[tid].get("was_online", False)

                if is_online and not was:
                    logger.info(f"[{self.log_tag} POLL] [{tv['nome']}] Voltou ONLINE! Aguardando boot...")
                    # Delay para TVs (especialmente webOS) inicializarem serviços de rede/tela
                    await asyncio.sleep(30)
                    logger.info(f"[{self.log_tag} POLL] [{tv['nome']}] Disparando launch pós-boot...")
                    await self.trigger_launch_if_ready(current_tv, source="polling")
                if not is_online and was:
                    logger.info(f"[{self.log_tag} POLL] [{tv['nome']}] Foi OFFLINE.")

                _tv_state[tid]["was_online"] = is_online
            except Exception as e:
                logger.debug(f"[{self.log_tag} POLL] [{tv['nome']}] Erro: {e}")

    # -- watchdog de app ativo ---------------------------------------------

    async def app_watchdog_tv(self, tv: dict, interval: int = 30):
        driver = self._driver(tv)
        if driver is None:
            return

        tid = tv["id"]
        ip = tv["ip"]
        logger.info(f"[{self.log_tag} WATCHDOG] [{tv['nome']}] Guardiao iniciado (intervalo: {interval}s).")
        await asyncio.sleep(interval)

        while True:
            try:
                current_tv = get_tv(tid)
                if current_tv is None or not current_tv.get("enabled") or not current_tv.get("watchdog", True):
                    await asyncio.sleep(interval)
                    continue
                if self._skip(current_tv):
                    await asyncio.sleep(interval)
                    continue

                online = await driver.is_online(ip)
                if not online:
                    _tv_state[tid]["watchdog_status"] = {
                        "last_check": time.time(), "active_app_id": None,
                        "our_app_active": None, "tv_online": False
                    }
                    await asyncio.sleep(interval)
                    continue

                active_id = await driver.active_app(ip)
                target_id = driver.watchdog_target_app(current_tv)
                our_active = bool(target_id) and (active_id == target_id)

                _tv_state[tid]["watchdog_status"] = {
                    "last_check": time.time(), "active_app_id": active_id,
                    "our_app_active": our_active, "tv_online": True
                }

                if our_active:
                    logger.debug(f"[{self.log_tag} WATCHDOG] [{tv['nome']}] App OK.")
                else:
                    logger.warning(
                        f"[{self.log_tag} WATCHDOG] [{tv['nome']}] App ativo: '{active_id}'. "
                        f"Relancando '{target_id}'..."
                    )
                    await self.trigger_launch_if_ready(current_tv, source="watchdog")

            except Exception as e:
                logger.debug(f"[{self.log_tag} WATCHDOG] [{tv['nome']}] Erro: {e}")

            await asyncio.sleep(interval)

    # -- SSDP listener ------------------------------------------------------

    async def ssdp_listener(self):
        """Escuta anuncios SSDP e dispara launch para TVs do filtro de marcas."""
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
        logger.info(f"[{self.log_tag} SSDP] Listener global ativo (marcas: {self.brands or 'todas'}).")

        while True:
            try:
                data, addr = await loop.run_in_executor(None, lambda: _recv_with_timeout(sock, 2048, 2.0))
                if data is None:
                    await asyncio.sleep(0.05)
                    continue
                sender_ip = addr[0]
                msg = data.decode("utf-8", errors="ignore").lower()
                for tv in load_tvs():
                    if tv["ip"] != sender_ip or not tv.get("enabled"):
                        continue
                    driver = self._driver(tv)
                    if driver is None or not driver.match_ssdp(msg):
                        continue
                    init_state(tv)
                    logger.info(
                        f"[{self.log_tag} SSDP] Anuncio de {sender_ip} -> [{tv['nome']}]. "
                        f"Disparando launch..."
                    )
                    await self.trigger_launch_if_ready(tv, source="SSDP")
            except Exception as e:
                if "timed out" not in str(e).lower():
                    logger.debug(f"[{self.log_tag} SSDP] Erro: {e}")
                await asyncio.sleep(0.05)

    # -- entry point --------------------------------------------------------

    async def start(self):
        """Inicia SSDP, scheduler, polling e watchdog para as TVs do filtro."""
        tvs = [t for t in load_tvs() if not self._skip(t)]
        brands = ", ".join(sorted(self.brands)) if self.brands else "todas"

        if not load_tvs():
            logger.info(f"[{self.log_tag}] Nenhuma TV cadastrada em tvs_config.json.")
        logger.info(f"[{self.log_tag}] Iniciando watchers ({len(tvs)} TV(s), marcas: {brands}).")

        try:
            asyncio.create_task(self.ssdp_listener())
        except Exception as e:
            logger.warning(f"[{self.log_tag}] SSDP nao iniciou: {e}. Usando apenas polling.")

        asyncio.create_task(self.scheduler())

        for tv in tvs:
            init_state(tv)
            asyncio.create_task(self.polling_watcher_tv(tv))
            if tv.get("watchdog", True):
                asyncio.create_task(self.app_watchdog_tv(tv))

        logger.info(f"[{self.log_tag}] Todos os watchers iniciados.")


# ---------------------------------------------------------------------------
# util SSDP (reutilizado pelo listener)
# ---------------------------------------------------------------------------

def _recv_with_timeout(sock: socket.socket, bufsize: int, timeout: float):
    sock.settimeout(timeout)
    try:
        return sock.recvfrom(bufsize)
    except (socket.timeout, Exception):
        return None, None


# ---------------------------------------------------------------------------
# Descoberta ativa de TVs (SSDP M-SEARCH + varredura TCP) - painel "Detectar"
# ---------------------------------------------------------------------------

_SUPPORTED_BRAND_LABELS = {"roku": "Roku", "webos": "LG webOS"}

# Alvos SSDP: dispositivos so respondem ao ST que conhecem. A Roku responde
# a "roku:ecp"; a LG webOS a "webos-second-screen"/MediaServer; "ssdp:all"
# cobre o resto (mas nao e confiavel em todas as TVs).
_SSDP_SEARCH_TARGETS = (
    "ssdp:all",
    "roku:ecp",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:device:MediaServer:2",
    "urn:schemas-upnp-org:service:ContentDirectory:1",
    "urn:lge-com:service:webos-second-screen:1",
)

# Fallback TCP: porta conhecida de cada protocolo (sem conexao invasiva:
# apenas abre/fecha o socket. Nao inicia pareamento.)
_SCAN_TARGETS = (
    ("roku", 8060),
    ("webos", 3001),
    ("webos", 3000),
)


def _local_ip() -> str:
    """IP LAN do servidor (sem enviar trafego, socket UDP)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def _local_networks() -> List[dict]:
    """Redes IPv4 reais do host: [{ip, prefix, network_start, network_end}].

    Usa Get-NetIPAddress (independente de idioma). Ignora loopback e redes
    grandes demais para escanear (> 4096 hosts, ex. /16).
    """
    nets = []
    own_ips = []
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object IPAddress, PrefixLength | ConvertTo-Json"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip()
        data = json.loads(out) if out else []
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            ip = (entry.get("IPAddress") or "").strip()
            prefix = int(entry.get("PrefixLength") or 0)
            if not ip or prefix <= 0:
                continue
            try:
                import socket as _s
                ip_int = struct.unpack("!I", _s.inet_aton(ip))[0]
            except OSError:
                continue
            size = 2 ** (32 - prefix)
            if size > 4096:  # redes /16, /12 etc: escaneaveis demais
                continue
            mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            net = ip_int & mask
            own_ips.append(ip)
            nets.append({
                "ip": ip,
                "prefix": prefix,
                "network_start": net + 1,
                "network_end": net + size - 1,
            })
    except Exception as e:
        logger.debug(f"[detect] Falha ao ler redes locais: {e}")
    return nets, own_ips


def _arp_table() -> Dict[str, str]:
    """IP -> MAC da tabela ARP (arp -a). Usado para preencher o MAC no
    cadastro (necessario para Wake-on-LAN na LG)."""
    table = {}
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return table
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ip = parts[0]
        mac = parts[1]
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            continue
        if re.match(r"^([0-9a-f]{2}-){5}[0-9a-f]{2}$", mac, re.I):
            table[ip] = mac.replace("-", ":").upper()
    return table


def _ssdp_parse_headers(text: str) -> dict:
    """Extrai cabecalhos basicos de uma resposta SSDP."""
    headers = {}
    for line in text.splitlines()[1:]:  # a primeira linha e o status HTTP
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.lower().strip()] = v.strip()
    return headers


def _http_server_banner(ip: str, port: int = 8060) -> Optional[str]:
    """Retorna a marca confirmada pelo Server HTTP (ex.: 'roku') ou None.

    Usado para confirmar que a porta 8060 aberta e mesmo uma Roku
    (evita falso positivo de outro servico HTTP na rede).
    """
    try:
        with socket.create_connection((ip, port), timeout=1.0) as s:
            s.sendall(
                f"GET / HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode("utf-8")
            )
            data = s.recv(1024)
        text = data.decode("utf-8", errors="ignore").lower()
        head = text.split("\r\n\r\n", 1)[0]
        return "roku" if "roku" in head else None
    except Exception:
        return None


async def _tcp_scan_network(brand_filter: tuple) -> List[dict]:
    """Varre as sub-redes reais do host nas portas conhecidas (Roku/LG).

    Usa a mascara real de cada interface (ex.: /22 cobre as TVs vizinhas),
    ignora o proprio IP, e confirma a porta 8060 via banner HTTP (evita
    falsos positivos de outros servicos). Descobre TVs mesmo quando o
    multicast SSDP e bloqueado. Abertura de socket TCP e inofensiva (nao
    dispara pareamento).
    """
    networks, own_ips = _local_networks()
    own_ips = set(own_ips)
    targets = [
        ip
        for net in networks
        for ip_int in range(net["network_start"], net["network_end"])
        for ip in [socket.inet_ntoa(struct.pack("!I", ip_int))]
        if ip not in own_ips
    ]
    if not targets:
        return []

    sem = asyncio.Semaphore(300)
    found: Dict[str, dict] = {}

    async def check(ip: str):
        async with sem:
            async def try_port(brand: str, port: int):
                try:
                    _, w = await asyncio.wait_for(
                        asyncio.open_connection(ip, port), timeout=0.3
                    )
                    w.close()
                    return brand, port
                except Exception:
                    return None

            # sondar todas as portas do host em paralelo (1 round por host)
            for task in asyncio.as_completed(
                [try_port(brand, port) for brand, port in _SCAN_TARGETS]
            ):
                res = await task
                if res is not None:
                    brand, port = res
                    found.setdefault(
                        ip,
                        {"brand": brand, "ip": ip, "port": port, "location": "", "source": "scan"},
                    )
                    return

    # em lotes para nao criar milhares de tasks de uma vez
    batch = 1000
    for i in range(0, len(targets), batch):
        chunk = targets[i:i + batch]
        await asyncio.gather(*[check(ip) for ip in chunk])

    # limpa falsos positivos: confirma Roku pela porta 8060 via banner
    for item in list(found.values()):
        if item["brand"] not in brand_filter:
            found.pop(item["ip"], None)
        elif item["port"] == 8060 and _http_server_banner(item["ip"]) != "roku":
            found.pop(item["ip"], None)

    arp = _arp_table()
    for item in found.values():
        item["mac"] = arp.get(item["ip"], "")
    return list(found.values())


def _ssdp_probe(timeout: float) -> dict:
    """Busca ativa SSDP enviando todas as ST conhecidas (pode falhar se o
    multicast for bloqueado — o fallback TCP cobre esse caso)."""
    found = {}
    local = _local_ip()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError:
        return found
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if local:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
            except OSError:
                pass
        sock.bind(("", 0))
    except OSError:
        pass

    # envia todos os alvos; dispositivos respondem ao que conhecem
    for _ in range(2):
        for st in _SSDP_SEARCH_TARGETS:
            msg = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {_SSDP_MULTICAST_IP}:{_SSDP_PORT}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 1\r\n"
                f"ST: {st}\r\n"
                "\r\n"
            )
            try:
                sock.sendto(msg.encode("utf-8"), (_SSDP_MULTICAST_IP, _SSDP_PORT))
            except OSError:
                pass
        time.sleep(0.2)

    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        ip = addr[0]
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            continue
        try:
            brand = _classify_ssdp(text.lower())
        except Exception:
            brand = None
        if brand is None:
            continue
        headers = _ssdp_parse_headers(text)
        loc = headers.get("location", "")
        port = 0
        if loc:
            try:
                port = urlparse(loc).port or 0
            except Exception:
                port = 0
        if ip not in found:
            found[ip] = {"brand": brand, "ip": ip, "port": port, "location": loc, "source": "ssdp"}
    try:
        sock.close()
    except Exception:
        pass
    return found


def _classify_ssdp(message: str) -> Optional[str]:
    """Classifica um anuncio SSDP (minusculo) por marca, via drivers."""
    from .drivers import get_driver, known_types

    for tip in known_types():
        driver = get_driver(tip)
        if driver is not None and driver.match_ssdp(message):
            return tip
    return None


async def discover_tvs(timeout: float = 2.5) -> List[dict]:
    """Detecta TVs de marcas conhecidas na rede (SSDP + varredura TCP).

    Estrategias em cascata:
      1. SSDP M-SEARCH com as ST especificas de Roku/LG/media.
      2. Varredura TCP do /24 do servidor nas portas 8060/3001/3000
         (funciona quando o multicast e bloqueado).

    Devolve lista de dicts:
      {brand, nome, ip, port, location, source, configured}

    Obs.: para nao incomodar TV LG nao pareada, nunca conecta no WebSocket
    durante a deteccao (nome fica generico; device_info so para Roku ECP,
    que e read-only).
    """
    loop = asyncio.get_event_loop()

    # 1) SSDP ativo (bloqueante, roda em executor)
    found = await loop.run_in_executor(None, _ssdp_probe, timeout)

    # 2) Fallback TCP (async, garantido nao bloquear o loop)
    scan = await _tcp_scan_network(brand_filter=tuple(_SUPPORTED_BRAND_LABELS.keys()))

    arp = _arp_table()
    for item in scan:
        found.setdefault(item["ip"], item)

    registered_ips = {t["ip"] for t in load_tvs()}
    results = []
    for ip, item in found.items():
        brand = item["brand"]
        if brand not in _SUPPORTED_BRAND_LABELS:
            continue
        nome = _SUPPORTED_BRAND_LABELS.get(brand, brand.title())
        driver = get_driver(brand)
        # enriquecimento de nome apenas para protocolos read-only (evita
        # disparar dialogo de pareamento numa LG so para listar).
        if driver is not None and brand != "webos":
            try:
                info = await asyncio.wait_for(driver.device_info(ip), timeout=3)
            except Exception:
                info = None
            if info:
                nome = info.get("friendly_name") or info.get("model_name") or nome
        item["nome"] = nome
        item["mac"] = item.get("mac") or arp.get(ip, "")
        item["configured"] = ip in registered_ips
        results.append(item)
    results.sort(key=lambda x: (x["configured"], x["brand"], x["ip"]))
    return results
