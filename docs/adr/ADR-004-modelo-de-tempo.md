# ADR-004: Modelo de tempo — segmentos, relógio de parede e anúncios

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
O pico é medido no relógio do chat (tempo de parede), mas o corte acontece no tempo do vídeo. Três fatores fazem esses relógios divergirem: (1) o streamlink demora a escrever o primeiro byte; (2) a Twitch tem intervalos de anúncio que o streamlink **remove** do arquivo; (3) quedas reiniciam a gravação.

## Decisão
- `wall_ts` é o instante de recepção de cada mensagem.
- A gravação é dividida em `RecordingSegment`s (`live_NN.ts`), cada um com `wall_start` = primeiro byte escrito e uma lista `ad_breaks` extraída do stderr do streamlink.
- `offset = (wall_ts − CHAT_DELAY_OFFSET_S) − wall_start − Σ anúncios anteriores`. Pico dentro de anúncio é descartado.
- A gravação é feita em **MPEG-TS** (sobrevive a crash) e o corte é feito direto do `.ts`.

## Consequências
- Clipes alinhados mesmo com anúncios e quedas.
- `CHAT_DELAY_OFFSET_S` e `CLIP_PRE_S`/`CLIP_POST_S` permitem calibrar empiricamente o atraso de reação do chat.
- A detecção do anúncio depende do texto do log do streamlink, que pode mudar entre versões, por isso a versão fica fixada em `requirements.txt`.
