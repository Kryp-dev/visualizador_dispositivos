/**
 * kiosk-webos/config.js - Configuracao do kiosk PBR para TVs LG webOS.
 *
 * A URL do visualizador e sempre passada pelo servidor via launch params
 * (PalmSystem.launchParams). Nao ha fallback intencional: se o app abrir
 * sem parametro do servidor, a tela de erro sera exibida.
 *
 * Para testes manuais sem o servidor, defina a URL aqui:
 *
 *   window.KIOSK_CONFIG = {
 *     url: "http://IP_DO_SERVIDOR:8080/api/roku/visualizador",
 *   };
 */
window.KIOSK_CONFIG = {
  url: "",          // vazio = obrigatorio vir via launch params do servidor
  reloadSec: 60,
  reloadOnlyVisible: true,
  showDebug: false,
  pingUrl: null,
  pingSecs: 30
};