/**
 * kiosk-webos/config.js - URL do visualizador que o kiosk abre.
 *
 * A primeira vez, o servidor abre o app passando a URL via launch params
 * (quando o campo "App kiosk nativo" do painel aponta para este app).
 * Se isso nao funcionar na sua TV, defina a URL manualmente aqui:
 *
 *   window.KIOSK_CONFIG = {
 *     url: "http://IP_DO_SERVIDOR:8080/api/roku/visualizador",
 *     reloadSec: 60,
 *     reloadOnlyVisible: true,
 *     showDebug: false,
 *     pingUrl: null,
 *     pingSecs: 30
 *   };
 */
window.KIOSK_CONFIG = {
  url: "https://seibtcomercial.github.io/VELOCIMETRO_SEIBT_-/",
  reloadSec: 60,
  reloadOnlyVisible: true,
  showDebug: false,
  pingUrl: null,
  pingSecs: 30
};