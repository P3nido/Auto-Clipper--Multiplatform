# ADR-006: Saída vertical 9:16 para redes sociais

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
Os clipes vão para Reels, Shorts e TikTok, que usam vídeo vertical 9:16 (1080×1920). As lives são 16:9, e na Twitch a webcam do streamer costuma ficar num canto, com o jogo ocupando o resto. Depois da otimização do ADR-005, o encode x264 em 1080p60 é o gargalo (~33 s por clipe de 30 s).

## Opções
| Layout | Como fica | Prós | Contras |
|---|---|---|---|
| **A: fundo desfocado** (`vertical_blur`) | vídeo inteiro na largura total, centralizado, sobre uma cópia ampliada e desfocada dele mesmo | não perde nada da tela (webcam, HUD, chat da live); é o padrão de clipes de stream | o vídeo ocupa só ~1/3 da altura |
| **B: corte central** (`vertical_crop`) | recorta a faixa central 9:16 e amplia | tela cheia, mais imersivo | perde as laterais, onde costumam ficar a webcam e o HUD |
| **C: horizontal** (`horizontal`) | 16:9 como está hoje | fiel à live | ruim no celular e nas redes verticais |

## Decisão
- `OUTPUT_LAYOUT = "vertical_blur"` é o padrão, com `vertical_crop` e `horizontal` disponíveis no `config.py`.
- O tamanho vertical é `VERTICAL_SIZE = (1080, 1920)`.
- `OUTPUT_FPS = 30`. Isso reduz pela metade os quadros a codificar nas lives de 60 fps, e 30 fps é o padrão das redes. `None` mantém o fps original.
- No layout com desfoque, o borrão é aplicado numa versão **reduzida** do vídeo e depois ampliado, o que é muito mais barato que desfocar em 1080×1920.
- A legenda tem estilo próprio para o vertical (`SUBTITLE_STYLE_VERTICAL`), maior e posicionada na faixa de baixo, fora do vídeo, para não cobrir o jogo.
- O filtro de vídeo é montado por uma função **pura** (`clipper.build_video_filter`), testável sem FFmpeg.

## Consequências
- Clipes prontos para postar, sem edição.
- O encode deve ficar mais rápido que o 1080p60 atual, por ter metade dos quadros. A medição fica registrada abaixo.
- O streamer que quiser a webcam em destaque (layout "facecam em cima, jogo embaixo") precisa de coordenadas da webcam por canal. Isso fica para o futuro, se fizer falta.

## Medição (2026-11-17, mesma amostra de 30 s, live 1080p60, CPU, mesma rodada)
| Saída | Render (corte + legenda) |
|---|---|
| horizontal, 60 fps (comportamento anterior) | 45,3 s |
| horizontal, 30 fps | 30,1 s |
| **vertical_blur, 30 fps (padrão)** | **36,7 s** (~19% mais rápido que antes) |
| vertical_crop, 30 fps | 36,1 s |

O ganho vem de `OUTPUT_FPS = 30`, aplicado **no início** do filtergraph, antes de escala, desfoque e sobreposição. Na 1ª versão o fps vinha depois da composição e o vertical_blur levava 41,4 s. O canvas 1080×1920 tem os mesmos pixels de um 1080p, então o vertical, sozinho, não acelera. Para acelerar mais: `VERTICAL_SIZE = (720, 1280)` (~2,2× menos pixels) ou `VIDEO_PRESET = "superfast"`.
