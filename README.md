# Automação Instagram e Facebook — Código da Virada

Este repositório executa somente este fluxo, mesmo com o computador desligado:

- Reels: 09:00 e 21:00, horário de Brasília;
- publicação independente no Instagram e na Página do Facebook;
- confirmação separada por rede, sem repetir a rede que já confirmou.

A fila é `fila/fila-reels.json`. Cada item aponta para um asset temporário da
release `fila-instagram-facebook`; ele não entra no histórico Git. O asset só
é removido depois da confirmação das duas redes.

O Drive continua sendo o acervo permanente. O computador local só repõe a fila
quando voltar a ficar ligado, copiando vídeos de
`03 - Finalizados/02 - Instagram e Facebook/01 - Prontos para Programar` e
movendo-os para `02 - Programados` assim que entram na fila.

## Legenda padrão

- Instagram e Facebook: `Siga @codigodavirada_br`

## Segredos do GitHub (Settings → Secrets and variables → Actions)

- `IG_ACCESS_TOKEN`
- `IG_BUSINESS_ID`
- `FB_PAGE_ACCESS_TOKEN`
- `FB_PAGE_ID`

Nenhuma senha ou credencial deve ser gravada no código, no Git ou no Drive.

## Arquitetura

Baseado no publicador `cristianoladik/como-jesus-cristo-faria-automacao`
(mesmo mecanismo usado também por `pzoadriana/ig-agendamento-github`), sem a
parte de Stories e YouTube — aqui é só Reels no Instagram e Facebook.
