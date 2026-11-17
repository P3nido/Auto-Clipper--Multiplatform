# Auto-Clipper Multiplataforma

Monitora um canal da **Twitch**, **YouTube** ou **Kick** só pela URL, grava a live automaticamente, encontra os momentos de maior engajamento no chat, recorta clipes de 30 s e queima legendas geradas localmente pelo Whisper. É **100% gratuito e local**: não precisa de conta, token nem chave de API.

> 🚧 **Status:** fase 0 (base de arquitetura). Os módulos ainda são esqueletos com contratos e testes. Veja o roteiro em [ARCHITECTURE.md §11](ARCHITECTURE.md#11-roteiro-de-implementação).

## Pipeline

1. **Monitorar e gravar:** a cada 30 s verifica se a live está no ar e grava em `best` com o Streamlink.
2. **Capturar o chat:** em paralelo e de forma anônima (IRC da Twitch, Pusher da Kick, chat-downloader no YouTube).
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

A primeira instalação baixa o PyTorch, que é pesado. O modelo do Whisper é baixado no primeiro uso.

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

Os resultados ficam em `sessions/<data>_<plataforma>_<canal>/final/`.

## Estrutura

```
config.py  main.py  session.py  recorder.py  chat_listener.py
hype_detector.py  clipper.py  transcriber.py  tests/  docs/adr/
```

Detalhes em [ARCHITECTURE.md](ARCHITECTURE.md).
