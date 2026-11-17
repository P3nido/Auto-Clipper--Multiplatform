# ADR-003: Concorrência com threading + Event

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
A gravação e o chat precisam rodar simultaneamente e encerrar juntos. A gravação é um subprocess (streamlink). As fontes de chat são bloqueantes (`socket`, `websocket-client`, gerador do `chat-downloader`).

## Opções
- **asyncio:** exigiria envolver as bibliotecas bloqueantes em `to_thread`, então seriam threads de qualquer forma, com mais complexidade.
- **threading:** casa com bibliotecas bloqueantes e tem um modelo mental simples.
- **multiprocessing:** isolamento que não precisamos aqui, e o IPC complica o `stop`.

## Decisão
`threading` com um único `threading.Event` de parada compartilhado. O `Recorder` seta o evento quando a live acaba, e o `main` seta no Ctrl+C. Toda espera bloqueante tem timeout de no máximo 2 s para checar o evento.

## Consequências
- O encerramento limpo fica garantido por contrato (≤ 5 s após o stop).
- O pós-processamento é sequencial e fica fora dessas threads.
