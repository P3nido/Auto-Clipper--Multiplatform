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
SPIKE_MULTIPLIER = 1.8

# Modelo do Whisper: "auto" (large-v3-turbo na GPU NVIDIA, small na CPU) ou
# "tiny", "base", "small", "medium", "large-v3-turbo", "large-v3".
# Quanto maior, melhor a legenda e mais lento.
WHISPER_MODEL = "auto"


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
MIN_MESSAGES_PER_BUCKET = 3     # piso absoluto, evita "pico" em chat quase parado
SPIKE_COOLDOWN_S = 30           # distância mínima entre dois picos
CHAT_DELAY_OFFSET_S = 0.0       # o chat reage depois do lance: desloca o pico p/ trás

# --- Etapa 4: recorte ---------------------------------------------------
CLIP_PRE_S = 15                 # segundos antes do pico
CLIP_POST_S = 15                # segundos depois do pico
MAX_CLIPS = 60                  # limite por live (com clipagem ao vivo: os primeiros 60)
FFMPEG_BIN = "ffmpeg"           # ou caminho completo, ex.: r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE_BIN = "ffprobe"
# Formato do clipe final (ADR-006):
#   "vertical_blur": 9:16 com o vídeo inteiro sobre fundo desfocado (padrão de clipes de stream)
#   "vertical_crop": 9:16 recortando o centro (tela cheia, perde as laterais)
#   "horizontal":    16:9 como a live
OUTPUT_LAYOUT = "vertical_blur"
VERTICAL_SIZE = (1080, 1920)    # largura x altura dos layouts verticais
OUTPUT_FPS: int | None = 30     # None = mantém o fps da live (60 fps dobra o tempo de encode)
VIDEO_CRF = 18                  # qualidade do re-encode (menor = melhor/maior)
VIDEO_PRESET = "veryfast"       # velocidade do x264: ultrafast ... veryslow (mais lento = menor)
AUDIO_BITRATE = "160k"          # áudio AAC dos clipes

# --- Etapa 5: transcrição e legenda -------------------------------------
WHISPER_ENGINE = "faster"       # "faster" (faster-whisper, ~4x mais rápido em CPU) ou "openai" (ADR-005)
WHISPER_COMPUTE_TYPE = "auto"   # "auto" | "float16" (GPU) | "int8" (CPU) | "float32"
WHISPER_VAD = True              # faster-whisper: pula trechos sem fala (música, silêncio)
# Legendas curtas, estilo redes sociais: cada bloco na tela tem no máximo
# SUBTITLE_MAX_CHARS caracteres (~2 linhas no vertical) e SUBTITLE_MAX_CUE_S segundos.
# Usa o tempo de cada palavra do Whisper; 0 desativa (volta às frases inteiras).
SUBTITLE_MAX_CHARS = 40
SUBTITLE_MAX_CUE_S = 3.0
WHISPER_LANGUAGE: str | None = "pt"  # None = detecta o idioma automaticamente (lives em outras línguas)
SUBTITLE_STYLE = (
    "FontName=Arial,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=40"
)
# Estilo nos layouts verticais (1080x1920, calibrado com frame real). Os tamanhos seguem a escala do
# libass: FontSize 10 ~ 67 px de altura de linha em 1920 (y*6,67), mas MarginL/R escalam em x (1080/384 = 2,8):
# 21 -> ~60 px laterais. Alignment=2 + MarginV=60 deixa o texto em y~1310-1520, na faixa desfocada
# logo abaixo do vídeo 16:9 (y 656-1264); no vertical_crop cai no terço inferior.
SUBTITLE_STYLE_VERTICAL = (
    "FontName=Arial,FontSize=10,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=0,"
    "Alignment=2,MarginV=60,MarginL=21,MarginR=21"
)

# --- Hardware / GPU (ADR-007) -------------------------------------------
# "auto" detecta e testa o hardware na inicialização (veja: python main.py --diagnostico)
VIDEO_ENCODER = "auto"          # "auto" | "nvenc" (NVIDIA) | "qsv" (Intel) | "amf" (AMD) | "cpu"
HW_DECODE = "auto"              # "auto" | "cuda" | "d3d11va" | "qsv" | "none"
WHISPER_DEVICE = "auto"         # "auto" | "cuda" | "cpu"
# (WHISPER_MODEL e WHISPER_COMPUTE_TYPE também aceitam "auto": large-v3-turbo/float16
#  na GPU NVIDIA, small/int8 na CPU)

# --- Várias lives (ADR-008) ---------------------------------------------
MAX_PARALLEL_LIVES = 4          # lives gravando/monitorando ao mesmo tempo
POSTPROCESS_WORKERS = 1         # clipes gerados um de cada vez (não disputar a GPU)

# --- Clipagem ao vivo (ADR-010) -----------------------------------------
LIVE_CLIPPING = True            # gera os clipes DURANTE a live (False = tudo só no fim)
LIVE_SCAN_INTERVAL_S = 30       # de quanto em quanto tempo procura picos novos no chat
LIVE_SAFETY_MARGIN_S = 5        # folga: o fim da janela precisa estar gravado com esta sobra

# --- Enviar ao Google Drive (ADR-011) -----------------------------------
# O app copia os clipes escolhidos para a pasta do Google Drive para desktop,
# que sobe para a nuvem sozinho. "auto" detecta "G:\Meu Drive" (ou "My Drive");
# também aceita qualquer pasta, ex.: r"D:\OneDrive\Clipes".
EXPORT_DIR = "auto"
EXPORT_AUTO = "auto"
EXPORT_SUBFOLDER = "Auto-Clipper"           # dentro do Drive: Meu Drive\Auto-Clipper\<live>\
DRIVE_ROOT_NAMES = ("Meu Drive", "My Drive")

# --- Saída --------------------------------------------------------------
SESSIONS_DIR = "sessions"
DELETE_RAW_AFTER_PROCESS = False   # apaga os .ts depois de gerar os clipes finais
LOG_LEVEL = "INFO"
