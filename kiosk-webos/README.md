# Kiosk LG webOS (app embarcado)

App web empacotado (IPK) que abre o **visualizador do painel** em tela cheia
na LG, com recarga periódica e medidas de kiosk (best effort — kiosk "inquebrável"
de verdade na webOS exige o licenciamento comercial **Signage/LG CMS**; aqui
usamos app próprio + o **watchdog do servidor**, que relança o app se alguém
sair com o controle).

## Como funciona o controle

O campo **"App kiosk nativo"** no painel (o valor deve ser
`com.enterposto.kiosk`) faz o servidor:

- **Launch**: abrir este app instalado em vez do navegador nativo.
- **Watchdog**: relançar este app se detectar que saíram dele.

Se o campo ficar **vazio**, o servidor volta a usar o navegador nativo
(`com.webos.app.browser`) apontando para a página — mais simples e suficiente
na maioria dos casos. Este app é a versão "hardened" opcional.

## Instalação (developer mode)

1. **Na TV**: instale o app **Developer Mode** na LG Content Store.
2. Na TV: ative o Developer Mode indicado (aceite o EULA, abra o terminal do
   dev mode e anote o IP — o app DevOps/CLI usa este mesmo IP + porta 9922).
3. **No PC**: instale o CLI da LG (webOS TV SDK) com Node:
   ```
   npm install -g @webos-tv-sdk/cli
   ```
   (ou use o instalador oficial do *webOS TV SDK* da LG).
4. Cadastre o dispositivo:
   ```
   ares-setup-device --add=<nome> --host=<IP_DA_TV> --port=9922 --username=prisoner
   ```
   (o usuário padrão do dev mode é `prisoner`; a senha é a exibida no app
   Developer Mode na TV).
5. Defina a URL do kiosk em `config.js` (ou deixe o servidor passar via
   launch params — tente sem configurar primeiro).
6. Empacote e instale:
   ```
   ares-package .
   ares-install --device=<nome> com.enterposto.kiosk_1.0.0_all.ipk
   ```
   (ou use `package.bat` na raiz deste diretório para gerar o IPK em `dist/`).

7. No **painel unificado**: edite a TV LG e preencha **App kiosk nativo** com
   `com.enterposto.kiosk`. Depois clique em **▶ App**. A TV deve abrir o app.

## Estrutura

- `appinfo.json` — metadados do app web (id/title/type).
- `index.html` — tela cheia com iframe do visualizador + reload + bloqueios.
- `config.js` — URL do kiosk e ajustes (ver comentários no arquivo).
- `icon.png` — ícone (placeholder, troque pelo da empresa se quiser).

## Desinstalar

```
ares-install --device=<nome> --remove com.enterposto.kiosk
```