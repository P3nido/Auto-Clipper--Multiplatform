# ADR-010: Clipagem ao vivo (durante a gravação)

**Status:** Aceito · **Data:** 2026-11-18

## Contexto
No primeiro uso real do `.exe`, o usuário acompanhou 4 lives por ~20 min e o app mostrava "0 picos". O detector funcionava: rodando sobre o chat já capturado, encontrou 7 picos claros, por exemplo 175 mensagens em 10 s contra uma média de 18. Mas a detecção só rodava **depois** que a live terminava (ADR-001/ADR-008). Lives duram horas, e para o usuário "já está na hora de clipar".

## Decisão
Durante a gravação, cada job varre o chat periodicamente e manda para a fila **cada pico assim que ele está pronto para ser cortado**:

- **Varredura** a cada `LIVE_SCAN_INTERVAL_S` (30 s): conta as mensagens do segmento atual até agora e roda o mesmo `detect_spike_buckets`, com a mesma média móvel causal, o mesmo warmup e o mesmo cooldown. A detecção ao vivo e a final dão o mesmo resultado.
- **Pico "fechado"** (não pode mais mudar) quando:
  1. já passou `SPIKE_COOLDOWN_S` desde o fim do bloco, e nenhuma rajada pode mais crescer a partir dele;
  2. o fim da janela do clipe (`pico + CLIP_POST_S`) já está gravado no `.ts`, com uma folga de `LIVE_SAFETY_MARGIN_S` (5 s).
- **Pico fechado** vira uma tarefa de clipe na **mesma fila única** (ADR-008): extrair áudio → Whisper → render. O FFmpeg lê o `.ts` que ainda está crescendo, só até o trecho já gravado.
- **Sem duplicatas:** janelas que se sobrepõem a um clipe já enfileirado são ignoradas. No fim da gravação, uma passada final só gera os picos que ainda não viraram clipe.
- `MAX_CLIPS` passa a valer como "os N primeiros momentos". Antes eram os N maiores, mas ao vivo não dá para conhecer o futuro.
- `LIVE_CLIPPING = False` volta ao comportamento antigo (tudo no fim).
- **Estado:** o job continua `RECORDING`, e `spikes`, `clips_total`, `clips_done` e `final_clips` sobem durante a gravação. A interface mostra os clipes e libera "Abrir clipes" assim que o primeiro fica pronto.

## Consequências
- Os clipes aparecem minutos depois do momento, e não horas.
- A GPU e a CPU trabalham durante a live. Com várias lives, a fila serializa (ADR-008).
- O ranking "os N maiores da live" deixa de existir. Isso pode ser revisitado com uma pontuação final que apague os clipes mais fracos.
