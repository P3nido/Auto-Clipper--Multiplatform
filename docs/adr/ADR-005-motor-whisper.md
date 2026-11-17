# ADR-005: Motor de transcrição e encode único por clipe

**Status:** Aceito (revisado em 2026-11-17 após as medições da fase 4) · **Data original:** 2026-11-17

## Contexto
O pedido especifica Whisper local (`base`/`small`, `pt`), e o alvo é CPU em Windows. A fase 4 mediu, com CPU sem GPU (PyTorch 2.15.0+cpu), um clipe real de 30 s em 1080p60:

| Etapa | Tempo |
|---|---|
| corte com re-encode | 37 s |
| openai-whisper `small` | 42 s (+21 s para carregar o modelo, uma vez por live) |
| queima da legenda, segundo re-encode | 27 s |
| **total** | **~107 s por clipe**, ~55 min para 30 clipes |

## Opções
| | openai-whisper | faster-whisper |
|---|---|---|
| Velocidade em CPU | 1× | ~4× (CTranslate2, int8) |
| Instalação | PyTorch (~1 GB+) | sem PyTorch (ctranslate2 + PyAV) |
| Qualidade | referência | equivalente (mesmos pesos convertidos) |
| Extras | — | filtro VAD embutido (pula música e silêncio) |

## Decisão
1. **faster-whisper é o motor padrão** (`WHISPER_ENGINE = "faster"`, `compute_type="int8"`, `vad_filter=True`). O openai-whisper continua disponível com `WHISPER_ENGINE = "openai"`, e a dependência dele (com PyTorch) passa a ser **opcional**.
2. **Um encode de vídeo por clipe:** extrai só o áudio da janela (rápido) → transcreve → `render_clip` corta e queima a legenda numa passada (ARCHITECTURE §8). Isso elimina o segundo re-encode e uma geração de perda de qualidade.

## Consequências
- Instalação bem mais leve (sem PyTorch no caminho padrão).
- `transcriber.burn_subtitles` sai; a renderização vai para `clipper.render_clip` (o clipper é o dono do FFmpeg de vídeo).
- O `.wav` temporário é apagado depois de cada clipe, e `clips/` deixa de guardar vídeos intermediários.
- As medições de antes e depois estão registradas abaixo.

## Medição antes/depois (2026-11-17, mesma amostra de 30 s, 1080p60, CPU)
| Etapa | Antes (openai-whisper + 2 encodes) | Depois (faster-whisper int8 + 1 encode) |
|---|---|---|
| extrair áudio | — | 1,0 s |
| transcrição `small` | 61,0 s | **5,9 s** (~10×) |
| vídeo | corte 31,2 s + queima 39,5 s | render único 32,9 s |
| **por clipe** | **~132 s** | **~40 s** (~3,3×) |
| carregar modelo (1×/live) | 9,8 s | 8,0 s |

O encode de vídeo (x264 1080p60) passa a ser o gargalo. A próxima alavanca é a saída em 720p ou vertical 9:16.
Nota: faster-whisper 1.2.1 é incompatível com PyAV 19 (`metadata_errors`), por isso `av<19` está fixado no requirements.
