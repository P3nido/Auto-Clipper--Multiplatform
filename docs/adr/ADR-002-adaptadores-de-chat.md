# ADR-002: Adaptadores de chat por plataforma

**Status:** Aceito (o adaptador do YouTube depende de spike de validação) · **Data:** 2026-11-17

## Contexto
O pedido priorizava `chat-downloader` como interface única e `pytchat` como alternativa. A verificação mostrou:
- `chat-downloader` 0.2.8 é de **set/2023** e **não suporta Kick** (só YouTube, Twitch, Zoom e Facebook).
- `pytchat` foi **arquivado em jan/2022**.
- O IRC anônimo da Twitch (`justinfan`) é estável e não precisa de biblioteca.
- O chat da Kick usa Pusher via WebSocket. O ID da sala vem de uma API atrás do Cloudflare.

## Decisão
| Plataforma | Implementação |
|---|---|
| Twitch | IRC próprio via `socket` (stdlib) |
| Kick | Pusher próprio (`websocket-client`) + `curl_cffi` para a API do canal |
| YouTube | `chat-downloader`. Plano B: cliente InnerTube próprio, se o spike falhar |

O `pytchat` foi descartado.

## Consequências
- Menos dependência de bibliotecas sem manutenção. A Twitch, plataforma principal, não depende de nenhuma.
- Mais código próprio para manter (Kick muda a chave Pusher ou o endpoint de vez em quando, por isso a chave fica em `config.py`).
- Ação: a fase 3 começa por um spike de 5 min com `chat-downloader` numa live real do YouTube.
