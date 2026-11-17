# Auto-Clipper Multiplataforma

Monitora um canal da **Twitch**, **YouTube** ou **Kick** só pela URL, grava a live automaticamente, encontra os momentos de maior engajamento no chat, recorta clipes de 30 s **na vertical (9:16), prontos para Reels, Shorts e TikTok**, e queima legendas geradas localmente pelo Whisper. É **100% gratuito e local**: não precisa de conta, token nem chave de API.

> ✅ **Status:** v1 funcional, com as 5 etapas implementadas e validadas em lives reais (Twitch, Kick e YouTube) e 165 testes. Próximos passos de desempenho em [ARCHITECTURE.md §10](ARCHITECTURE.md#10-riscos-conhecidos).

## Pipeline

1. **Monitorar e gravar:** a cada 30 s verifica se a live está no ar e grava em `best` com o Streamlink.
2. **Capturar o chat:** em paralelo e de forma anônima (IRC da Twitch, histórico do chat da Kick e API interna do chat do YouTube).
3. **Detectar picos:** blocos de 10 s, média móvel, limiar de `SPIKE_MULTIPLIER`× e cooldown.
4. **Recortar:** 15 s antes e 15 s depois de cada pico, com o FFmpeg.
5. **Legendar:** Whisper local gera o `.srt`, e o FFmpeg queima a legenda no vídeo final.

## Pré-requisitos

| Ferramenta | Para quê | Como instalar (Windows) |
|---|---|---|
| Python 3.10+ | tudo | python.org |
| **FFmpeg + FFprobe no PATH** | recorte e legendas | `winget install Gyan.FFmpeg` |
| Google Chrome ou Chromium | só **Kick** (desafio do Cloudflare) | já instalado na maioria das máquinas |

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

O modelo do Whisper (faster-whisper) é baixado no primeiro uso, ~460 MB para o `small`. O motor openai-whisper é opcional e puxa o PyTorch (veja requirements.txt).

## Uso

1. Abra `config.py` e preencha `STREAM_URL`. Se quiser, ajuste `SPIKE_MULTIPLIER` e `WHISPER_MODEL`.
2. Rode:

```bash
python main.py
```

| Comando | Efeito |
|---|---|
| `python main.py` | monitora para sempre e processa cada live que acontecer |
| `python main.py --once` | encerra depois da primeira live |
| `python main.py --url https://kick.com/canal` | ignora a URL do `config.py` |
| `python main.py --process sessions/<pasta>` | refaz só os clipes de uma gravação já feita |
| `python main.py --max-duration 3600` | para de gravar depois de N segundos e processa o que gravou |

**Ctrl+C** durante a gravação para tudo de forma limpa e ainda gera os clipes do que foi gravado.

Os resultados ficam em `sessions/<data>_<plataforma>_<canal>/final/`, com os nomes `clip_01_seg0_12m30s.mp4` (posição no vídeo gravado). O log completo fica no `run.log` da mesma pasta.

## Dicas

- **Formato do clipe:** `OUTPUT_LAYOUT = "vertical_blur"` (padrão: vídeo inteiro sobre fundo desfocado, nada da tela é perdido), `"vertical_crop"` (tela cheia, corta as laterais e costuma cortar a webcam) ou `"horizontal"` (16:9 como a live).
- **Tamanho da legenda:** os blocos têm no máximo `SUBTITLE_MAX_CHARS = 40` caracteres (~2 linhas) e `SUBTITLE_MAX_CUE_S = 3.0` segundos. `SUBTITLE_MAX_CHARS = 0` volta às frases inteiras do Whisper.
- **Live em outro idioma:** use `WHISPER_LANGUAGE = None` em `config.py` para o Whisper detectar o idioma. Forçar `"pt"` numa live em francês gera legenda vazia ou sem sentido.
- **Poucos ou muitos clipes:** ajuste `SPIKE_MULTIPLIER` (menor = mais sensível) e `MIN_MESSAGES_PER_BUCKET` (piso para chats pequenos).
- **Clipe começa tarde demais:** o chat reage depois do lance. Aumente `CLIP_PRE_S` ou `CHAT_DELAY_OFFSET_S`.
- **Tempo de processamento:** em CPU, cada clipe de 30 s leva ~40–50 s (faster-whisper `small` + 1 encode em 1080×1920 a 30 fps). O gargalo é o encode do vídeo: `VERTICAL_SIZE = (720, 1280)` ou `VIDEO_PRESET = "superfast"` aceleram bastante.
- **Disco:** ~3 GB por hora em `best`. Use `STREAM_QUALITY = "720p"` ou `DELETE_RAW_AFTER_PROCESS = True`.
- **FFmpeg recém-instalado:** se o terminal ainda não enxerga o `ffmpeg`, o programa encontra sozinho o atalho do winget.

## Estrutura

```
config.py  main.py  session.py  recorder.py  chat_listener.py
hype_detector.py  clipper.py  transcriber.py  tests/  docs/adr/
```

Detalhes em [ARCHITECTURE.md](ARCHITECTURE.md).
