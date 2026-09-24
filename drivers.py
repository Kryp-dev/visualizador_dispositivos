"""
drivers.py - Interface de driver de TV (marca/protocolo) e registro.

Um "driver" encapsula tudo que difere entre fabricantes e protocolos:

  - Roku   (tipo "roku")  : ECP por HTTP na porta 8060
  - LG     (tipo "webos") : webOS via WebSocket (porta 3000) + WoL para ligar
  - futuro (tipo "samsung"): Tizen via WebSocket (porta 8001/8002)

O nucleo compartilhado em base.py (scheduler, polling, watchdog, SSDP, CRUD)
so enxerga a interface TvDriver. Para adicionar uma nova marca basta criar um
modulo que defina a classe, chame register_driver() e disponibilize um router.
"""

from typing import Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class TvDriver(Protocol):
    """Contrato minimo que todo driver de TV deve implementar."""

    # Chave usada no campo "tipo" do tvs_config.json (ex.: "roku", "webos").
    brand: str

    async def is_online(self, ip: str) -> bool:
        """True se a TV responde E esta com a tela ligada."""
        ...

    async def is_reachable(self, ip: str) -> bool:
        """True se a TV responde na rede (independente do estado da tela)."""
        ...

    async def device_info(self, ip: str) -> Optional[dict]:
        """Informacoes basicas do dispositivo (modelo, versao, etc) ou None."""
        ...

    async def set_power(self, tv: dict, on: bool) -> bool:
        """Liga (on=True) ou desliga (on=False) a TV.

        Recebe o dict completo da TV para permitir estrategias que dependem
        de configuracao (ex.: Wake-on-LAN a partir do campo "mac")."""
        ...

    async def launch(self, tv: dict) -> bool:
        """Abre o conteudo kiosk/monitoramento nesta TV."""
        ...

    async def query_apps(self, ip: str) -> Optional[list]:
        """Lista os apps instalados (lista de dicts com id/name) ou None."""
        ...

    async def active_app(self, ip: str) -> Optional[str]:
        """ID do app atualmente em primeiro plano ou None."""
        ...

    def watchdog_target_app(self, tv: dict) -> Optional[str]:
        """ID do app que deve ficar em primeiro plano (None = nao vigiar)."""
        ...

    def match_ssdp(self, message: str) -> bool:
        """True se o anuncio SSDP (minusculo) indica um aparelho desta marca."""
        ...


# Registro de drivers por marca/tipo: {"roku": RokuDriver, "webos": WebOSDriver}
_DRIVER_REGISTRY: Dict[str, TvDriver] = {}


def register_driver(driver: TvDriver) -> None:
    """Registra um driver para um tipo de TV. Sobrescreve se ja existir."""
    _DRIVER_REGISTRY[driver.brand] = driver


def get_driver(tipo: str) -> Optional[TvDriver]:
    """Retorna o driver registrado para o tipo, ou None se desconhecido."""
    return _DRIVER_REGISTRY.get(tipo)


def known_types() -> tuple:
    """Tipos (marcas) com driver registrado neste processo."""
    return tuple(_DRIVER_REGISTRY.keys())
