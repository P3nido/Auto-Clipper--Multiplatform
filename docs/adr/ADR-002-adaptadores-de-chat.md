# ADR-002: Adaptadores de chat por plataforma

**Status:** Aceito, **revisado em 2026-11-17** após os testes da fase 3 · **Data original:** 2026-11-17

## Contexto
O pedido priorizava o `chat-downloader` como interface única e o `pytchat` como alternativa. A verificação mostrou:
- `pytchat` foi **arquivado em jan/2022**.
- `chat-downloader` 0.2.8 (set/2023) **não suporta Kick** e, testado numa live real do YouTube, **falha**: `ParsingError: Unable to parse initial video data`.
- **Kick abandonou o Pusher público**: as chaves conhecidas (`32cbd69e…`, `eb1d5f28…`) e a encontrada no HTML (`01a1e9af…`) retornam `4001 App key not in this cluster` em todos os clusters. O tempo real novo (`web.kick.com/api/v1/realtime/...`, `websockets.kick.com/viewer/v1/token`) exige token de sessão de navegador (403 anônimo).
- O endpoint **`web.kick.com/api/v1/chat/<channel_id>/history`** responde anonimamente (via `curl_cffi`) com as 25 mensagens mais recentes e um `cursor` que pagina para trás sem sobreposição.
- O **InnerTube do YouTube** (`/live_chat` + `youtubei/v1/live_chat/get_live_chat`) responde sem conta. A chave e a versão do cliente vêm da própria página.
- O **IRC anônimo da Twitch** (`justinfan`) é estável e não precisa de biblioteca.

## Decisão
| Plataforma | Adaptador | Transporte | Dependência |
|---|---|---|---|
| Twitch | `TwitchIRCSource` | IRC com tags (`tmi-sent-ts`) | stdlib |
| Kick | `KickHistorySource` | consulta o histórico a cada 1,5 s + cursor para não perder mensagens | `curl_cffi` |
| YouTube | `YouTubeInnerTubeSource` | consulta a InnerTube seguindo as continuations | `curl_cffi` |

Saem do projeto o `chat-downloader`, o `pytchat` e o `websocket-client`.

**Tempo das mensagens:** as três plataformas informam o instante de **envio**, que passa a ser o `wall_ts`. Isso é mais preciso que a recepção, principalmente nos adaptadores por consulta, que recebem as mensagens em lotes.

## Consequências
- Uma dependência só (`curl_cffi`) para Kick e YouTube, e nenhuma biblioteca abandonada no caminho.
- Kick por consulta tem um teto: 25 msgs por página × 11 páginas a cada 1,5 s ≈ 180 msgs/s antes de perder mensagens. Isso está bem acima de lives grandes.
- São endpoints internos, não documentados, e podem mudar. Cada adaptador falha com erro claro no log, o `ChatListener` reconecta, e a gravação **nunca** depende do chat.
- Relógio: depende de o relógio local estar sincronizado (NTP do Windows). Um `wall_ts` mais de 2 s no futuro é limitado a `now()`.
