"""Configuração do Auto-Clipper Multiplataforma.

Para o uso normal, só é preciso editar os três valores da seção "BÁSICO".
Os demais têm padrões sensatos e estão documentados no ARCHITECTURE.md.
"""

# ════════════════════════════════ BÁSICO ════════════════════════════════

# URL do canal. Exemplos:
#   https://www.twitch.tv/nome_do_canal
#   https://www.youtube.com/@nome_do_canal/live
#   https://kick.com/nome_do_canal
STREAM_URL = "https://www.twitch.tv/NOME_DO_CANAL"

# Um bloco de 10 s vira "pico" quando tem X vezes mais mensagens que a média recente.
SPIKE_MULTIPLIER = 3.0

# Modelo do Whisper: "tiny", "base", "small", "medium", "large-v3".
# Quanto maior, melhor a legenda e mais lento. Em CPU, "small" é um bom equilíbrio.
WHISPER_MODEL = "small"


# ═══════════════════════════════ AVANÇADO ═══════════════════════════════

# --- Etapa 1: monitoramento e gravação ---------------------------------
POLL_INTERVAL_S = 30            # intervalo entre checagens de "está ao vivo?"
STREAM_QUALITY = "best"         # qualidade do streamlink ("best", "720p", "480p"...)
STREAMLINK_EXTRA_ARGS: list[str] = []  # ex.: ["--kick-low-latency"]
# Se o streamlink cair, por quanto tempo seguir tentando reabrir a gravação
# antes de considerar que a live acabou (streamer caiu e voltou, rede oscilou).
RECORDING_RESUME_GRACE_S = 90

# --- Etapa 2: chat ------------------------------------------------------
CHAT_RECONNECT_MAX_BACKOFF_S = 60   # espera máxima entre tentativas de reconexão
KICK_POLL_INTERVAL_S = 1.5          # consulta ao histórico do chat da Kick
KICK_MAX_BACKFILL_PAGES = 10        # páginas extras (25 msgs cada) se o chat acelerar
YOUTUBE_MAX_POLL_INTERVAL_S = 5.0   # teto da espera entre consultas ao chat do YouTube

# --- Etapa 3: detecção de picos ----------------------------------------
BUCKET_SECONDS = 10             # tamanho de cada bloco de contagem
BASELINE_WINDOW_BUCKETS = 30    # média móvel sobre os últimos 30 blocos (= 5 min)
WARMUP_BUCKETS = 6              # ignora o primeiro 1 min (média ainda instável)
MIN_MESSAGES_PER_BUCKET = 5     # piso absoluto, evita "pico" em chat quase parado
SPIKE_COOLDOWN_S = 30           # distância mínima entre dois picos
CHAT_DELAY_OFFSET_S = 0.0       # o chat reage depois do lance: desloca o pico p/ trás

# --- Etapa 4: recorte ---------------------------------------------------
CLIP_PRE_S = 15                 # segundos antes do pico
CLIP_POST_S = 15                # segundos depois do pico
MAX_CLIPS = 30                  # limite por live (mantém os picos mais fortes)
FFMPEG_BIN = "ffmpeg"           # ou caminho completo, ex.: r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE_BIN = "ffprobe"
VIDEO_CRF = 18                  # qualidade do re-encode (menor = melhor/maior)

# --- Etapa 5: transcrição e legenda -------------------------------------
WHISPER_ENGINE = "openai"       # "openai" (padrão). "faster" fica reservado (ADR-005)
WHISPER_LANGUAGE = "pt"
SUBTITLE_STYLE = (
    "FontName=Arial,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=40"
)

# --- Saída --------------------------------------------------------------
SESSIONS_DIR = "sessions"
DELETE_RAW_AFTER_PROCESS = False   # apaga os .ts depois de gerar os clipes finais
LOG_LEVEL = "INFO"
