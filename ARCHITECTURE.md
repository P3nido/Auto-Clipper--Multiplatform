# Arquitetura — Auto-Clipper Multiplataforma

> Documento vivo. As decisões que justificam cada escolha estão em [`docs/adr/`](docs/adr/).

## 1. Visão geral

Um processo Python local, sem contas nem chaves de API, que:

1. **monitora** uma URL (Twitch, YouTube ou Kick) a cada 30 s;
2. **grava** a live com o Streamlink e, **em paralelo**, **captura o chat** anonimamente;
3. ao fim da live, **detecta picos** de volume de chat em janelas de 10 s;
4. **recorta** 30 s ao redor de cada pico com o FFmpeg;
5. **transcreve** cada clipe com o Whisper local e **queima a legenda** no vídeo final.

```
                    ┌──────────────── main.py (orquestrador) ────────────────┐
                    │                                                        │
  STREAM_URL ──► is_live()? ──não──► dorme POLL_INTERVAL_S ──┐              │
                    │ sim                                     │              │
                    ▼                                         │              │
            Session.create()  ──► sessions/<id>/              │              │
                    │                                         │              │
        ┌───────────┴────────────┐   stop_event (threading.Event)            │
        ▼                        ▼                                           │
  [Thread] Recorder        [Thread] ChatListener                             │
  streamlink → live_NN.ts  ChatSource(plataforma) → chat.jsonl               │
  registra segmentos e     reconecta com backoff                             │
  intervalos de anúncio                                                      │
        │ live acabou ─► seta stop_event ─► ChatListener encerra             │
        └───────────┬────────────┘                                           │
                    ▼                                                        │
            ─── PÓS-PROCESSAMENTO (também roda isolado: --process <dir>) ─── │
            hype_detector.find_spikes(session)      → spikes.json            │
            clipper.plan_clips() + cut_clip()       → clips/clip_NN.mp4      │
            transcriber.transcribe_to_srt()         → subs/clip_NN.srt       │
            transcriber.burn_subtitles()            → final/clip_NN.mp4      │
                    └────────────────────────────── volta a monitorar ───────┘
```

## 2. Estrutura de pastas

Projeto **plano** (todos os módulos na raiz), como pedido. A única abstração formal é a porta `ChatSource`, porque é ali que existem implementações trocáveis (uma por plataforma). Ver [ADR-001](docs/adr/ADR-001-estrutura-plana.md).

```
auto-clipper/
├── config.py            # ÚNICO arquivo que o usuário edita (URL, multiplicador, modelo Whisper)
├── main.py              # CLI + loop de monitoramento + orquestração das 5 etapas
├── session.py           # Session/RecordingSegment: pastas, manifesto session.json, relógio
├── recorder.py          # Etapa 1 — is_live() e Recorder (Streamlink em subprocess)
├── chat_listener.py     # Etapa 2 — ChatMessage, porta ChatSource, adaptadores, ChatListener
├── hype_detector.py     # Etapa 3 — lógica PURA de janelas, média móvel, limiar e cooldown
├── clipper.py           # Etapa 4 — planejamento e corte dos clipes (FFmpeg)
├── transcriber.py       # Etapa 5 — Whisper → .srt e queima da legenda (FFmpeg)
├── tests/               # pytest — foco em lógica pura (hype_detector, clipper.plan_clips)
├── docs/adr/            # Registros de decisão de arquitetura
├── sessions/            # SAÍDA (fora do git) — uma pasta por live gravada
├── requirements.txt / requirements-dev.txt
├── CLAUDE.md            # Convenções para os agentes (orquestrador, coder, qa-explorer)
└── .claude/             # agents/ (coder, qa-explorer) e skills/ do projeto
```

### Layout de uma sessão (saída)

```
sessions/20261117-213000_twitch_canal/
├── session.json         # manifesto: url, plataforma, canal, segmentos, status de cada etapa
├── run.log              # log completo desta sessão
├── live_00.ts           # segmento de gravação (MPEG-TS: aguenta crash/queda)
├── live_01.ts           # novo segmento se o streamlink cair e a live continuar
├── chat.jsonl           # 1 mensagem por linha, gravada em tempo real
├── spikes.json          # picos detectados (pós-live)
├── clips/clip_00.mp4    # 30 s recortados
├── subs/clip_00.srt     # legenda gerada pelo Whisper
└── final/clip_00.mp4    # clipe com legenda queimada → pronto para redes sociais
```

## 3. Responsabilidades e contratos dos módulos

Dependências fluem num sentido só: `main → (recorder, chat_listener, hype_detector, clipper, transcriber) → session → config`. Os módulos de etapa **não importam uns aos outros**. A única exceção é `clipper`, que importa o *tipo* `Spike` de `hype_detector`.

| Módulo | Expõe | Faz I/O? |
|---|---|---|
| `session.py` | `Session`, `RecordingSegment`, `now()` | disco (manifesto) |
| `recorder.py` | `is_live(url)`, `Recorder`, `remux_to_mp4()`, `RecorderError` | subprocess, disco |
| `chat_listener.py` | `ChatMessage`, `ChatSource` (+ `TwitchIRCSource`, `KickHistorySource`, `YouTubeInnerTubeSource`), `detect_platform()`, `channel_from_url()`, `make_chat_source()`, `ChatListener` | rede, disco |
| `hype_detector.py` | `Spike`, `bucketize()`, `detect_spike_buckets()`, `find_spikes()` | só `find_spikes` lê disco |
| `clipper.py` | `ClipSpec`, `plan_clips()`, `cut_clip()`, `probe_duration()`, `ClipperError` | subprocess |
| `transcriber.py` | `load_model()`, `transcribe_to_srt()`, `burn_subtitles()`, `TranscriberError` | modelo, subprocess |
| `main.py` | `main()`, `monitor_loop()`, `run_session()`, `postprocess()` | orquestra |

Os esqueletos `.py` já contêm as assinaturas, dataclasses e docstrings, e eles são o contrato que o subagente `coder` implementa.

## 4. Modelo de tempo (o ponto mais delicado)

O pico é detectado pelo **relógio do chat**, mas o corte é feito pelo **tempo do vídeo**. Se esses dois relógios divergirem, o clipe sai deslocado. Ver [ADR-004](docs/adr/ADR-004-modelo-de-tempo.md).

- Toda mensagem recebe `wall_ts = time.time()` **no momento da recepção**.
- Cada `RecordingSegment` guarda `wall_start` = instante em que **o primeiro byte** foi escrito no `.ts`, e não o instante em que o streamlink foi iniciado (a diferença pode passar de 5 s).
- **Anúncios da Twitch:** o Streamlink filtra os anúncios, então o arquivo "pula" esse trecho e fica mais curto que o tempo de parede. O `Recorder` lê o stderr do streamlink (`Detected advertisement break of N seconds`) e registra `ad_breaks` no segmento. A conversão `wall_ts → offset no vídeo` subtrai os anúncios anteriores, e um pico **dentro** de um anúncio é descartado.
- **Quedas:** se o streamlink sair e a live continuar, abre-se um novo segmento (`live_01.ts`). Um pico pertence ao segmento cujo intervalo de parede o contém.
- **Atraso de reação:** o chat reage *depois* do lance. `CHAT_DELAY_OFFSET_S` (padrão 0) desloca o pico para trás, e `CLIP_PRE_S`/`CLIP_POST_S` (padrão 15/15) podem ficar assimétricos, por exemplo 20/10.

`offset_video = (wall_ts − CHAT_DELAY_OFFSET_S) − segment.wall_start − Σ(anúncios anteriores no segmento)`

## 5. Detecção de picos (algoritmo)

Implementado como funções puras em `hype_detector.py`, totalmente testáveis sem live.

1. `bucketize`: conta mensagens por bloco contíguo de `BUCKET_SECONDS` (10 s), indexado a partir do `wall_start` do segmento. Blocos vazios entram com contagem 0.
2. Linha de base de cada bloco *i*: média dos `BASELINE_WINDOW_BUCKETS` blocos **anteriores** (o bloco atual fica de fora, senão ele mesmo infla a média).
3. O bloco *i* é pico se: `i ≥ WARMUP_BUCKETS` **e** `count ≥ SPIKE_MULTIPLIER × base` **e** `count ≥ MIN_MESSAGES_PER_BUCKET`. O piso absoluto evita que 1→4 mensagens num chat parado conte como "hype".
4. Cooldown: candidatos a menos de `SPIKE_COOLDOWN_S` do candidato **anterior** formam uma mesma rajada, que vai se encadeando. Da rajada fica só o bloco de maior contagem (no empate, o primeiro). Assim, um hype longo e contínuo vira **um** clipe, e não vários clipes colados.
5. O instante do pico é o **centro** do bloco.

## 6. Concorrência e encerramento

`threading` + um único `threading.Event` (`stop_event`). Ver [ADR-003](docs/adr/ADR-003-concorrencia-threading.md).

- O `Recorder` roda numa thread e bloqueia no subprocess do streamlink. Quando a live acaba, ele **seta** `stop_event`.
- O `ChatListener` roda numa thread e confere `stop_event` entre mensagens e a cada timeout de socket (≤ 2 s).
- **Ctrl+C:** `main` seta `stop_event`, envia `terminate()` ao streamlink e espera as threads (`join` com timeout). Mesmo assim roda o pós-processamento do que já foi gravado.
- O pós-processamento é **sequencial** depois da live. O Whisper usa a CPU inteira, e paralelizar não compensa.

## 7. Captura de chat por plataforma

Ver [ADR-002](docs/adr/ADR-002-adaptadores-de-chat.md). Todas as plataformas produzem o mesmo `ChatMessage(platform, author, text, wall_ts)`.

| Plataforma | Adaptador | Como funciona | Dependência |
|---|---|---|---|
| Twitch | `TwitchIRCSource` | IRC anônimo (`NICK justinfan<n>`, tags) em `irc.chat.twitch.tv:6667`, responde `PING` | só stdlib (`socket`) |
| Kick | `KickHistorySource` | consulta `web.kick.com/api/v1/chat/<channel_id>/history` a cada 1,5 s e pagina pelo `cursor` quando o chat acelera (o Pusher foi desativado) | `curl_cffi` (Cloudflare) |
| YouTube | `YouTubeInnerTubeSource` | InnerTube: `/live_chat` + `youtubei/v1/live_chat/get_live_chat` seguindo as continuations (o chat-downloader está quebrado) | `curl_cffi` |

O `wall_ts` de cada mensagem é o instante de **envio** informado pela plataforma (`tmi-sent-ts`, `created_at`, `timestampUsec`), e não o de recepção. Os adaptadores por consulta recebem em lotes, e usar a recepção agruparia mensagens em blocos errados.

Reconexão: o `ChatListener` envolve qualquer fonte com backoff exponencial de 1 s até `CHAT_RECONNECT_MAX_BACKOFF_S`, registrando cada queda no log. Lacunas no chat aparecem como blocos com 0 mensagens, o que **reduz** a linha de base e nunca gera pico falso.

## 8. Recorte e legendas

- `plan_clips` (pura): converte picos em janelas `[pico − PRE, pico + POST]`, ajusta os limites ao `[0, duração]` do segmento, **funde** janelas sobrepostas e limita a `MAX_CLIPS` (mantém os picos de maior contagem).
- `cut_clip`: `ffmpeg -ss <ini> -i live_NN.ts -t <dur> -c:v libx264 -crf 18 -preset veryfast -c:a aac`. Esse re-encode garante um corte preciso no frame, e sem ele a legenda sairia desalinhada.
- `transcribe_to_srt`: Whisper com `language=WHISPER_LANGUAGE`, gerando os segmentos em SRT.
- `burn_subtitles`: `-vf subtitles=<srt>:force_style=<SUBTITLE_STYLE>`. **No Windows**, o caminho dentro do filtro precisa de escape (`C\:/...`). Para evitar isso, o ffmpeg roda com `cwd=` na pasta da sessão e o filtro usa caminho relativo.

## 9. Tratamento de erros

| Falha | Comportamento |
|---|---|
| URL sem plugin no streamlink | erro fatal na partida (`main` valida antes do loop) |
| Kick bloqueado pelo Cloudflare | log de erro com instrução (instalar Chrome). O loop continua tentando |
| Streamlink cai com a live online | novo segmento e a gravação segue |
| Chat cai | reconexão com backoff. A gravação nunca para por causa do chat |
| Falha em um clipe (corte/Whisper/queima) | registra o erro, marca no `session.json` e passa ao próximo clipe |
| Crash no meio | o `.ts` e o `chat.jsonl` já estão no disco. `python main.py --process sessions/<id>` refaz o pós-processamento |

## 10. Riscos conhecidos

1. **Endpoints internos (Kick e YouTube)** não são documentados e podem mudar sem aviso. Isso já aconteceu duas vezes: o `chat-downloader` quebrou e a Kick desativou o Pusher (ver ADR-002). *Mitigação:* adaptadores pequenos e isolados, erro claro no log e a gravação independente do chat.
2. **Kick + Cloudflare:** tanto a gravação (Streamlink precisa de Chrome/Chromium) quanto o chat (`curl_cffi` imitando o Chrome) dependem de passar pelo desafio.
3. **Disco:** medido na fase 2 (Twitch 1080p60 em `best`): cerca de 0,83 MB/s, ou seja, **~3 GB por hora** e ~12 GB numa live de 4 h. Para reduzir, use `STREAM_QUALITY = "720p"` ou `DELETE_RAW_AFTER_PROCESS`.
4. **CPU (medido na fase 4, 1080p60, só CPU):** cada clipe de 30 s leva **~107 s** (corte 37 s + Whisper `small` 42 s + queima 27 s), mais 21 s para carregar o modelo uma vez por live. Com `MAX_CLIPS = 30`, são ~55 min de pós-processamento. Otimizações já identificadas, em ordem de ganho:
   - **a)** corte e legenda num **único encode**, passando ao Whisper só o áudio da janela: economiza ~35 s por clipe e uma geração de perda;
   - **b)** **faster-whisper** (ADR-005): ~4× mais rápido na transcrição;
   - **c)** saída em 720p ou vertical 9:16 (formato de redes sociais): encode bem mais leve.

## 11. Roteiro de implementação

| Fase | Entrega | Responsável |
|---|---|---|
| 0 | Base: arquitetura, esqueletos, config, agents, skills | orquestrador ✅ |
| 1 | `hype_detector` + `clipper.plan_clips` + `wall_to_offset` (puros) com testes verdes | coder → qa-explorer ✅ |
| 2 | `session` + `recorder` (gravar uma live real da Twitch por 2 min) | coder → qa-explorer ✅ (90 s reais gravados, TS válido, stop em < 1 s) |
| 3 | `chat_listener`: Twitch IRC → Kick → spike do YouTube | coder → qa-explorer ✅ (chat real: Twitch 38 msgs/40 s, Kick 88 msgs/75 s com 1 reconexão, YouTube 7 msgs/75 s) |
| 4 | `clipper.cut_clip` + `transcriber` (com um `.ts` de amostra) | coder → qa-explorer ✅ (clipe real legendado em PT; ~107 s/clipe em CPU) |
| 5 | `main`: loop, Ctrl+C, `--process`, `--once`, teste ponta a ponta | coder → qa-explorer → revisão ✅ (live real → 2 picos → 2 clipes legendados) |
