# Automação Instagram e Facebook — Código da Virada

Este repositório executa estes fluxos, mesmo com o computador desligado:

- Reels conforme a rampa de horários gravada na fila;
- um pacote diário de Stories agendado às 09:00, com partes sequenciais de até 59 segundos;
- publicação independente no Instagram e na Página do Facebook;
- confirmação separada por rede, sem repetir a rede que já confirmou.

## Estado das redes

Instagram e Facebook estão **ativos**. O Facebook publica desde 05/09/2026,
quando `pages_manage_posts` foi liberado em Standard Access; no publicador, isso
é representado por `FACEBOOK_ATIVO = True`. Registros antigos com status
`pausado` pertencem ao período anterior à liberação e não representam o estado
atual da automação.

Reels são normalmente verificados nos minutos 07, 17, 27, 37, 47 e 57, além
da tentativa prioritária das 09:00. Na hora das 09h, há somente a recuperação
das 09:07; a janela seguinte fica livre para Stories, verificados uma vez por hora no minuto 10,
inclusive às 09:10 para o pacote agendado às 09:00. Cada fluxo tem seu grupo de
concorrência, para uma execução frequente de Reels não substituir um Story
pendente; o lock remoto Git CAS serializa os dois workflows e os escritores do
PC. Se uma execução atrasar, a próxima recupera em ordem cronológica até dez
Reels; Stories recuperam no máximo um pacote por dia, sem
despejar a fila acumulada. Em caso de erro, o processamento
para no item ou na parte com problema e o retoma sem repetir a rede confirmada.

## Como saber se houve publicação

O selo verde do workflow informa que a verificação da fila terminou sem erro;
sozinho, ele não comprova uma publicação. Abra o **Summary** da execução e
confira o estado explícito:

- `PUBLICADO`: a Meta confirmou uma ou mais publicações novas; o resumo mostra
  as quantidades e os IDs separados de Instagram e Facebook;
- `NENHUM_REEL_DEVIDO`: a fila foi verificada, mas nada foi publicado;
- `NENHUM_STORY_DEVIDO`: a fila de Stories foi verificada, mas nada foi
  publicado;
- `RECONCILIADO_SEM_NOVA_PUBLICACAO`: um estado antigo da fila foi concluído,
  sem uma nova chamada de publicação;
- `DIAGNOSTICO_META_OK`: a conta BUSINESS do Instagram e o Page Token foram
  confirmados em modo somente leitura, sem publicar nem limpar mídia;
- `FALHA`: houve erro; a execução fica vermelha e o resumo identifica a rede e
  o motivo.

Na página do job, somente um dos passos de resultado é executado com um desses
nomes. Considere uma execução como prova de postagem apenas quando o Summary
mostrar `PUBLICADO` e os IDs confirmados pela Meta.

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

## Stories

A fila independente é `fila/fila-stories.json`. Há no máximo um pacote por dia,
sempre às 09:00 de Brasília. A Action o verifica a partir das 09:10, depois da tentativa do Reel matinal. Um pacote contém uma ou mais partes em ordem
contínua (`1..N`), com no máximo dez partes; cada uma tem duração máxima de 59 segundos e estado separado
para Instagram e Facebook. O pacote só fica `concluido` quando todas as partes
foram confirmadas nas duas redes.

O workflow **Verificar fila e publicar Stories — Instagram e Facebook** salva a
fila depois de cada tentativa em cada rede. Depois, persiste o resultado no Git
antes de remover os assets temporários e faz uma segunda persistência para
guardar os marcadores da limpeza.

No Facebook, o retorno do upload não basta: a automação pesquisa o `post_id` ou
`video_id` no edge de Stories da própria Página e só conclui quando a Meta
devolve `PUBLISHED` (ou `ARCHIVED` numa reconciliação histórica). Em timeout,
grava `incerto` e apenas reconcilia os mesmos
IDs nas próximas execuções, sem reenviar o vídeo.

Todos os escritores automáticos de Reels e Stories usam a ref efêmera
`lock-publicacao-instagram-facebook` como mutex remoto. Depois de adquirir o
lock, atualizam `main`; somente então leem a fila ou alteram a Release. A
liberação compara o SHA do proprietário e nunca apaga o lock de outro processo.

No Drive, o fluxo operacional do Código da Virada usa as pastas fornecidas pelo
responsável:

1. `Story/Vídeos para Story` — vídeos de origem;
2. `Story/Cortados - Preparados` — pacotes com partes de até 59 segundos;
3. `Story/Postados - Agendados` — destino local após confirmação da publicação.

A fila foi criada vazia. Nenhum Story é publicado até que o repositor local
prepare um pacote, envie suas partes à Release e grave o agendamento na fila.

### Visibilidade da fila

O repositório e a Release são públicos, então os assets futuros podem ser
acessados antes da publicação. Essa exposição é conhecida e foi aceita pelo
responsável pelo projeto em 08/09/2026; o fluxo não precisa ser tornado privado
por esse motivo.

O Drive continua sendo o acervo permanente. O computador local só repõe a fila
quando voltar a ficar ligado, copiando vídeos de
`03 - Finalizados/02 - Instagram e Facebook/01 - Prontos para Programar` e
movendo-os para `02 - Programados` assim que entram na fila.

## Legenda padrão

- Instagram e Facebook: `Siga @codigodavirada_br`

`@codigodavirada_br` é o arroba atual e deve permanecer na legenda. O arquivo
de vídeo pode conter visualmente o arroba anterior `@codigo_da_virada_oficial`:
Reels legados continuam autorizados e o publicador não os rejeita por isso.

## Segredos do GitHub (Settings → Secrets and variables → Actions)

- `IG_ACCESS_TOKEN`
- `IG_BUSINESS_ID`
- `FB_PAGE_ACCESS_TOKEN`
- `FB_PAGE_ID`

Nenhuma senha ou credencial deve ser gravada no código, no Git ou no Drive.

## Arquitetura

Baseado no publicador `cristianoladik/como-jesus-cristo-faria-automacao`
(mesmo mecanismo usado também por `pzoadriana/ig-agendamento-github`), com
filas e workflows separados para Reels e Stories. O projeto mantém somente a
identidade, as contas, os caminhos e os estados do Código da Virada; nenhum
arquivo de fila ou credencial do projeto Jesus é compartilhado.
