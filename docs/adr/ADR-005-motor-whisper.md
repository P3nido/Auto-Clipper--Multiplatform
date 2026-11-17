# ADR-005: Motor de transcrição

**Status:** Proposto · **Data:** 2026-11-17

## Contexto
O pedido especifica OpenAI Whisper local (`base`/`small`, `pt`). O alvo é CPU em Windows.

## Opções
| | openai-whisper | faster-whisper |
|---|---|---|
| Velocidade em CPU | 1× | ~4× (CTranslate2, int8) |
| Instalação | puxa PyTorch (~1 GB+) | sem PyTorch |
| Qualidade | referência | equivalente (mesmos pesos) |
| Precisa de ffmpeg | sim | não (usa PyAV) |

## Decisão
Começar com **openai-whisper**, como pedido, isolado atrás de `transcriber.load_model()`/`transcribe_to_srt()`, que são as únicas funções que conhecem o motor. Trocar para faster-whisper vira uma alteração em um arquivo, acionada por `WHISPER_ENGINE` em `config.py` se a velocidade incomodar.

## Consequências
- A primeira instalação é pesada (PyTorch).
- Revisitar depois da fase 4, com tempos medidos.
