# Automação Instagram e Facebook — Código da Virada

Este repositório executa somente este fluxo, mesmo com o computador desligado:

- publicação conforme a rampa de horários gravada na fila, no horário de Brasília;
- publicação independente no Instagram e na Página do Facebook;
- confirmação separada por rede, sem repetir a rede que já confirmou.

O workflow verifica a fila a cada dez minutos, nos minutos 07, 17, 27, 37, 47
e 57. Essa frequência reduz os atrasos do agendador do GitHub. Se uma execução
atrasar ou não acontecer, a próxima processa os Reels vencidos em ordem
cronológica, até dez itens por vez. Em caso de erro, o processamento para no
item com problema e o retoma na execução seguinte.

## Rampa de publicação

| Dias desde 05/09/2026 | Reels por dia | Horários de Brasília |
|---|---:|---|
| 0 a 6 | 2 | 09:00 e 21:00 |
| 7 a 14 | 3 | 05:00, 09:00 e 21:00 |
| 15 a 29 | 4 | 05:00, 09:00, 13:00 e 21:00 |
| 30 em diante | 5 | 05:00, 09:00, 13:00, 17:00 e 21:00 |

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
