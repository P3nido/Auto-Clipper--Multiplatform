"""Interface gráfica do Auto-Clipper (ADR-008).

Cliente do `engine.JobManager`: o usuário cola o link da live e o app cuida do resto.
A interface só OBSERVA o motor por `JobStatus` (snapshots imutáveis). O callback
`on_update` do motor vem de outras threads, então é repassado ao thread da UI por um
`Signal` (nunca mexemos em widgets fora do thread principal).

Uso:
    python gui.py          # motor real
    python gui.py --demo   # FakeJobManager: 3 lives simuladas, sem gravar nada

Paleta (regra do projeto): só #101010, #ffffff e o acento #c8ff00. Estados se
diferenciam por texto, peso, opacidade e pelo acento — nada de semáforo colorido.
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QEventLoop, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import config
import exporter
import runtime
import uploader
from chat_listener import channel_from_url, detect_platform
from engine import JobState, JobStatus
from session import Session

__all__ = [
    "FakeJobManager", "MainWindow", "JobCard", "SettingsDialog", "ExportDialog",
    "YouTubeDialog", "load_recorded_session", "load_settings", "save_settings", "apply_settings", "main",
]

logger = logging.getLogger(__name__)

# ─────────────────────────── constantes de visual ───────────────────────────
BG = "#101010"
SURFACE = "#1a1a1a"
SURFACE_HOVER = "#202020"
BORDER = "#2a2a2a"
FG = "#ffffff"
FG_DIM = "rgba(255,255,255,170)"      # texto secundário (~66%)
FG_FAINT = "rgba(255,255,255,110)"    # legendas/rótulos (~43%)
ACCENT = "#c8ff00"
ACCENT_DIM = "rgba(200,255,0,90)"     # "respiro" do indicador AO VIVO

FONT_DISPLAY = "Bahnschrift SemiBold Condensed"   # títulos e números (vem com o Windows 10+)
FONT_BODY = "Segoe UI"
FONT_FALLBACKS = ("Bahnschrift", "Segoe UI", "Arial")

RADIUS_PX = 4
MIN_WINDOW_SIZE = (900, 680)
CARD_BUTTON_WIDTH = 132
STAT_BLOCK_WIDTH = 104
PULSE_INTERVAL_MS = 700
DISK_REFRESH_MS = 30_000
DISK_WARN_GB = 20
GB = 1024 ** 3
MB = 1024 ** 2
SHUTDOWN_TIMEOUT_S = 90

SETTINGS_PATH = runtime.data_dir() / "settings.json"
EXAMPLE_URL = "https://www.twitch.tv/nome_do_canal"
FFMPEG_INSTALL_CMD = "winget install Gyan.FFmpeg"

PLATFORM_LABELS = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}
ACTIVE_STATES = (JobState.WAITING, JobState.RECORDING, JobState.QUEUED, JobState.PROCESSING)
FINISHED_STATES = (JobState.DONE, JobState.STOPPED, JobState.ERROR)

# Ajustes do usuário -> valores de config.py
LAYOUT_CHOICES = (
    ("Vertical com fundo desfocado", "vertical_blur"),
    ("Vertical cortado (tela cheia)", "vertical_crop"),
    ("Horizontal (como a live)", "horizontal"),
)
LANGUAGE_CHOICES = (("Português", "pt"), ("Detectar automaticamente", None))
SENSITIVITY_CHOICES = (("Baixa (só momentos enormes)", 3.0), ("Média", 2.2), ("Alta (recomendado)", 1.8), ("Máxima", 1.5))
YOUTUBE_PRIVACY_CHOICES = (("Público", "public"), ("Não listado", "unlisted"), ("Privado", "private"))
SETTINGS_KEYS = ("output_layout", "whisper_language", "spike_multiplier", "sessions_dir",
                 "export_dir", "youtube_privacy", "youtube_auto_upload")
README_URL = "https://github.com/P3nido/Auto-Clipper--Multiplatform#postar-no-youtube"
YOUTUBE_PRIVATE_WARNING = ("O YouTube deixou o vídeo como privado. Projetos novos podem precisar "
                           "de verificação do Google.")


def default_settings() -> dict:
    """Padrões = valores ATUAIS do config.py (o settings.json só guarda o que o
    usuário mudou na tela de Ajustes; sem ele, vale o config.py)."""
    return {
        "output_layout": config.OUTPUT_LAYOUT,
        "whisper_language": config.WHISPER_LANGUAGE,
        "spike_multiplier": float(config.SPIKE_MULTIPLIER),
        "sessions_dir": config.SESSIONS_DIR,
        "export_dir": config.EXPORT_DIR,
        "youtube_privacy": config.YOUTUBE_PRIVACY,
        "youtube_auto_upload": bool(config.YOUTUBE_AUTO_UPLOAD),
    }

STATE_TITLES = {
    JobState.WAITING: "Aguardando a live",
    JobState.RECORDING: "● AO VIVO",     # o único destaque em caixa alta: é o sinal principal
    JobState.QUEUED: "Na fila",
    JobState.PROCESSING: "Gerando clipes",
    JobState.DONE: "Pronto",
    JobState.STOPPED: "Parado",
    JobState.ERROR: "Algo deu errado",
}


# ────────────────────────────── formatação (pt-BR) ──────────────────────────
def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_size(num_bytes: int) -> str:
    if num_bytes >= GB:
        return f"{num_bytes / GB:.1f} GB".replace(".", ",")
    return f"{num_bytes / MB:.0f} MB"


def format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


# ─────────────────────────────── ajustes (settings.json) ─────────────────────
def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """Lê settings.json; arquivo ausente ou corrompido vira os padrões."""
    settings = default_settings()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in SETTINGS_KEYS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning("settings.json inválido em %s; usando os padrões", path)
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    Path(path).write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_settings(settings: dict) -> None:
    """Aplica os ajustes sobre o módulo `config` (config.py em disco não é reescrito)."""
    config.OUTPUT_LAYOUT = settings["output_layout"]
    config.WHISPER_LANGUAGE = settings["whisper_language"]
    config.SPIKE_MULTIPLIER = float(settings["spike_multiplier"])
    config.SESSIONS_DIR = settings["sessions_dir"]
    config.EXPORT_DIR = settings["export_dir"]
    config.YOUTUBE_PRIVACY = settings["youtube_privacy"]
    config.YOUTUBE_AUTO_UPLOAD = bool(settings["youtube_auto_upload"])


# ─────────────────────────────── utilidades ──────────────────────────────────
def open_folder(path: Path) -> None:
    """Abre a pasta no Explorer (ou no gerenciador de arquivos do sistema)."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def open_readme_section() -> None:
    """Abre o passo a passo de "Postar no YouTube": o README.md ao lado do app, se existir;
    senão a página do projeto no GitHub."""
    readme = runtime.app_dir() / "README.md"
    if readme.is_file():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(readme)))
    else:
        QDesktopServices.openUrl(QUrl(README_URL))


def ffmpeg_available() -> bool:
    """FFmpeg e FFprobe acessíveis (PATH ou o atalho do winget, que não entra no PATH na hora)."""
    localappdata = os.environ.get("LOCALAPPDATA")
    for binary, exe in ((config.FFMPEG_BIN, "ffmpeg.exe"), (config.FFPROBE_BIN, "ffprobe.exe")):
        if shutil.which(binary):
            continue
        link = Path(localappdata) / "Microsoft" / "WinGet" / "Links" / exe if localappdata else None
        if sys.platform == "win32" and link is not None and link.exists():
            continue
        return False
    return True


def free_disk_gb(path: str | Path) -> float | None:
    """Espaço livre (GB) no disco da pasta, subindo até um pai que exista."""
    candidate = Path(path).resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free / GB
    except OSError:
        return None


def detect_hardware_text() -> str:
    """Texto do encoder para o rodapé. Roda fora do thread da UI; qualquer falha vira 'CPU'."""
    try:
        import hardware
        profile = hardware.detect()
        text = profile.video_encoder.upper()
        if profile.gpu_names:
            text += f" ({profile.gpu_names[0]})"
        return text
    except Exception:  # noqa: BLE001 - o rodapé nunca pode derrubar a UI
        logger.info("Detecção de hardware indisponível; mostrando CPU", exc_info=True)
        return "CPU"


# ────────────────────────────── gerenciador falso ────────────────────────────
class FakeJobManager:
    """Imita o JobManager para desenvolver/testar a UI e para demonstrações.

    Mesma API pública do engine.JobManager. Com `auto=True` (modo --demo) as lives
    evoluem sozinhas a cada segundo; nos testes, use `update()` para forçar estados.
    """

    def __init__(self, on_update: Callable[[JobStatus], None] | None = None, *,
                 seed: bool = False, auto: bool = False, clips_dir: Path | None = None) -> None:
        self._on_update = on_update
        self._jobs: dict[str, JobStatus] = {}
        self._ids = itertools.count(1)
        self._clips_dir = clips_dir or Path(tempfile.gettempdir()) / "auto-clipper-demo"
        self._timer: QTimer | None = None
        self._ticks = 0
        if seed:
            self._seed()
        if auto:
            self._timer = QTimer()
            self._timer.timeout.connect(self._tick)
            self._timer.start(1000)

    # --- API do contrato ---
    def add(self, url: str, *, once: bool = True, max_duration: float | None = None) -> str:
        platform = detect_platform(url)
        channel = channel_from_url(url)
        if any(j.channel.lower() == channel.lower() and j.platform == platform
               and j.state in ACTIVE_STATES for j in self._jobs.values()):
            raise ValueError("Essa live já está na lista.")
        if sum(j.state in ACTIVE_STATES for j in self._jobs.values()) >= config.MAX_PARALLEL_LIVES:
            raise RuntimeError(f"Limite de {config.MAX_PARALLEL_LIVES} lives ao mesmo tempo.")
        return self._create(url, platform, channel, JobState.WAITING, "Verificando se já começou")

    def stop(self, job_id: str) -> None:
        job = self._jobs[job_id]
        if job.state == JobState.WAITING:
            self.update(job_id, state=JobState.STOPPED, message="Parado")
        elif job.state == JobState.RECORDING:
            self.update(job_id, state=JobState.QUEUED, message="Gravação encerrada, na fila de clipes")

    def remove(self, job_id: str) -> None:
        if self._jobs[job_id].state in FINISHED_STATES:
            del self._jobs[job_id]

    def status(self, job_id: str) -> JobStatus:
        return self._jobs[job_id]

    def all(self) -> list[JobStatus]:
        return list(self._jobs.values())

    def shutdown(self, wait: bool = True) -> None:
        if self._timer is not None:
            self._timer.stop()

    # --- apoio a testes/demo ---
    def update(self, job_id: str, **changes) -> JobStatus:
        status = dataclasses.replace(self._jobs[job_id], updated_at=time.time(), **changes)
        self._jobs[job_id] = status
        if self._on_update is not None:
            self._on_update(status)
        return status

    def _create(self, url: str, platform: str, channel: str, state: JobState, message: str,
                **extra) -> str:
        job_id = f"fake{next(self._ids):02d}"
        status = JobStatus(job_id=job_id, url=url, platform=platform, channel=channel,
                           state=state, message=message, updated_at=time.time(), **extra)
        self._jobs[job_id] = status
        if self._on_update is not None:
            self._on_update(status)
        return job_id

    def _ensure_demo_clips(self, count: int = 12) -> None:
        """Cria clipes de mentira (arquivos pequenos) para a janela de envio funcionar na demo."""
        try:
            self._clips_dir.mkdir(parents=True, exist_ok=True)
            for i in range(1, count + 1):
                path = self._clips_dir / f"clip_{i:02d}.mp4"
                if not path.exists():
                    path.write_bytes(b"auto-clipper demo" * 1024 * i)
        except OSError:
            logger.warning("Não consegui criar os clipes de demonstração em %s", self._clips_dir)

    def _seed(self) -> None:
        self._ensure_demo_clips()
        self._create("https://www.twitch.tv/gaules", "twitch", "gaules", JobState.RECORDING,
                     "Gravando vídeo e chat", recorded_s=4350, recorded_bytes=3_600_000_000,
                     chat_messages=12480, spikes=7, clips_done=2, clips_total=3,
                     final_clips=(self._clips_dir / "clip_01.mp4", self._clips_dir / "clip_02.mp4"))
        self._create("https://kick.com/casimito", "kick", "casimito", JobState.PROCESSING,
                     "Gerando clipes e legendas", recorded_s=7260, recorded_bytes=6_100_000_000,
                     chat_messages=30210, spikes=8, clips_done=3, clips_total=8)
        self._create("https://www.youtube.com/@alanzoka/live", "youtube", "alanzoka",
                     JobState.WAITING, "Verificando se já começou")

    def _tick(self) -> None:
        """Evolui a demo: a live grava, a fila anda e o que termina vira PRONTO."""
        self._ticks += 1
        for job in self.all():
            if job.state == JobState.RECORDING:
                changes: dict = {}
                # Clipagem ao vivo (ADR-010): cada pico vira clipe pouco depois.
                if self._ticks % 25 == 0:
                    changes["spikes"] = job.spikes + 1
                    changes["clips_total"] = job.clips_total + 1
                elif job.clips_done < job.clips_total and self._ticks % 7 == 0:
                    self._clips_dir.mkdir(parents=True, exist_ok=True)
                    done = job.clips_done + 1
                    changes["clips_done"] = done
                    changes["final_clips"] = job.final_clips + (self._clips_dir / f"clip_{done:02d}.mp4",)
                self.update(job.job_id, recorded_s=job.recorded_s + 1,
                            recorded_bytes=job.recorded_bytes + 830_000,
                            chat_messages=job.chat_messages + 6,
                            message="Gravando vídeo e chat", **changes)
            elif job.state == JobState.PROCESSING and self._ticks % 3 == 0:
                done = job.clips_done + 1
                if done >= job.clips_total:
                    self._clips_dir.mkdir(parents=True, exist_ok=True)
                    clips = tuple(self._clips_dir / f"clip_{i:02d}.mp4" for i in range(1, done + 1))
                    self.update(job.job_id, state=JobState.DONE, clips_done=done,
                                message=f"{done} clipes prontos", final_clips=clips,
                                session_dir=self._clips_dir)
                else:
                    self.update(job.job_id, clips_done=done)


# ───────────────────────────────── componentes ───────────────────────────────
class UpdateBridge(QObject):
    """Leva JobStatus de threads do motor para o thread da UI."""
    updated = Signal(object)


class HardwareBridge(QObject):
    detected = Signal(str)


class ExportBridge(QObject):
    """Leva o progresso da thread de cópia para o thread da UI."""
    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)


class YouTubeBridge(QObject):
    """Leva os UploadEvent da thread da fila de envio para o thread da UI."""
    event = Signal(object)


class LoginBridge(QObject):
    """Resultado do login do Google (thread gui-yt-login): texto vazio = deu certo."""
    finished = Signal(str)


class StatBlock(QWidget):
    """Número grande (fonte condensada) com legenda pequena embaixo."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setFixedWidth(STAT_BLOCK_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.value = QLabel("–")
        self.value.setObjectName("statValue")
        self.caption = QLabel(caption)
        self.caption.setObjectName("statCaption")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class JobCard(QFrame):
    """Cartão de uma live. Só mostra o JobStatus; ações saem por signals."""

    stop_requested = Signal(str)
    open_requested = Signal(str)
    export_requested = Signal(str)       # menu Enviar > Google Drive
    youtube_requested = Signal(str)      # menu Enviar > YouTube
    remove_requested = Signal(str)

    def __init__(self, status: JobStatus) -> None:
        super().__init__()
        self.setObjectName("jobCard")
        self.job_id = status.job_id
        self._pulse_on = True
        self._build()
        self._pulse = QTimer(self)
        self._pulse.setInterval(PULSE_INTERVAL_MS)
        self._pulse.timeout.connect(self._toggle_pulse)
        self.update_status(status)

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 16, 16, 16)
        root.setSpacing(16)

        info = QVBoxLayout()
        info.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.channel_label = QLabel()
        self.channel_label.setObjectName("channel")
        self.platform_label = QLabel()
        self.platform_label.setObjectName("platformTag")
        head.addWidget(self.channel_label)
        head.addWidget(self.platform_label, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch(1)
        info.addLayout(head)

        self.state_label = QLabel()
        self.state_label.setObjectName("stateLabel")
        info.addWidget(self.state_label)
        self.message_label = QLabel()
        self.message_label.setObjectName("message")
        self.message_label.setWordWrap(True)
        info.addWidget(self.message_label)
        self.youtube_label = QLabel()
        self.youtube_label.setObjectName("message")
        self.youtube_label.setWordWrap(True)
        self.youtube_label.setVisible(False)
        info.addWidget(self.youtube_label)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        info.addSpacing(4)
        info.addWidget(self.progress)

        stats = QHBoxLayout()
        stats.setSpacing(0)
        self.stats = {
            "size": StatBlock("tamanho"), "chat": StatBlock("mensagens no chat"),
            "spikes": StatBlock("picos"), "clips": StatBlock("clipes"),
        }
        for block in self.stats.values():
            stats.addWidget(block)
        stats.addStretch(1)
        self.stats_row = QWidget()
        self.stats_row.setLayout(stats)
        stats.setContentsMargins(0, 8, 0, 0)
        info.addWidget(self.stats_row)
        root.addLayout(info, 1)

        # O tempo gravado é o elemento de destaque do cartão (a única "ousadia" do visual).
        clock = QVBoxLayout()
        clock.setSpacing(0)
        self.time_value = QLabel("0:00:00")
        self.time_value.setObjectName("bigTime")
        self.time_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.time_caption = QLabel("gravados")
        self.time_caption.setObjectName("statCaption")
        self.time_caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock.addWidget(self.time_value)
        clock.addWidget(self.time_caption)
        clock.addStretch(1)
        self.clock = QWidget()
        self.clock.setLayout(clock)
        root.addWidget(self.clock)

        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        self.stop_button = QPushButton("Parar")
        self.open_button = QPushButton("Abrir clipes")
        self.export_button = QPushButton("Enviar ▾")
        send_menu = QMenu(self.export_button)
        self.drive_action = send_menu.addAction("Google Drive")
        self.youtube_action = send_menu.addAction("YouTube")
        self.export_button.setMenu(send_menu)
        self.remove_button = QPushButton("Remover")
        self.remove_button.setObjectName("ghost")
        for button in (self.stop_button, self.open_button, self.export_button, self.remove_button):
            button.setFixedWidth(CARD_BUTTON_WIDTH)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.job_id))
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.job_id))
        self.drive_action.triggered.connect(lambda: self.export_requested.emit(self.job_id))
        self.youtube_action.triggered.connect(lambda: self.youtube_requested.emit(self.job_id))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.job_id))

    # --- estado ---
    def update_status(self, status: JobStatus) -> None:
        self.status = status
        state = status.state
        self.channel_label.setText(status.channel)
        self.platform_label.setText(PLATFORM_LABELS.get(status.platform, status.platform.upper()))

        title = STATE_TITLES[state]
        if state == JobState.PROCESSING and status.clips_total:
            title += f", clipe {min(status.clips_done + 1, status.clips_total)} de {status.clips_total}"
        self.state_label.setText(title)
        message = (status.error or status.message) if state == JobState.ERROR else status.message
        message = message.replace(" · ", ", ")   # a UI mostra dados em campos próprios, não em strings coladas
        self.message_label.setText(message)
        self.message_label.setVisible(bool(message))

        self._set_property(self, "state", state.name.lower())
        self._pulse_on = True
        self._set_property(self.state_label, "pulse", "on")
        if state == JobState.RECORDING:
            self._pulse.start()
        else:
            self._pulse.stop()

        # Progresso só enquanto gera clipes (barra "ocupada" até o total ser conhecido).
        self.progress.setVisible(state == JobState.PROCESSING)
        if state == JobState.PROCESSING:
            if status.clips_total:
                self.progress.setRange(0, status.clips_total)
                self.progress.setValue(status.clips_done)
            else:
                self.progress.setRange(0, 0)

        has_numbers = status.recorded_s > 0 or status.chat_messages > 0
        self.stats_row.setVisible(has_numbers)
        self.clock.setVisible(has_numbers)
        self.time_value.setText(format_duration(status.recorded_s))
        self.stats["size"].set_value(format_size(status.recorded_bytes))
        self.stats["chat"].set_value(format_count(status.chat_messages))
        self.stats["spikes"].set_value(format_count(status.spikes))
        self.stats["clips"].set_value(f"{status.clips_done} de {status.clips_total}")
        self.stats["clips"].setVisible(status.clips_total > 0)

        self.stop_button.setVisible(state in (JobState.WAITING, JobState.RECORDING))
        self.remove_button.setVisible(state in FINISHED_STATES)
        self.open_button.setVisible(state not in (JobState.WAITING, JobState.ERROR))
        has_clips = bool(status.final_clips)
        self.open_button.setEnabled(has_clips)
        self.open_button.setToolTip("" if has_clips else "Os clipes aparecem aqui quando ficarem prontos.")
        self.export_button.setVisible(state not in (JobState.WAITING, JobState.ERROR))
        self.export_button.setEnabled(has_clips)
        self.export_button.setToolTip("" if has_clips else "Os clipes aparecem aqui quando ficarem prontos.")
        self._set_property(self.open_button, "primary", "true" if state == JobState.DONE and has_clips else "false")

    def set_youtube_text(self, text: str) -> None:
        """Texto discreto do andamento da postagem no YouTube (vazio esconde)."""
        self.youtube_label.setText(text)
        self.youtube_label.setVisible(bool(text))

    @staticmethod
    def _set_property(widget: QWidget, name: str, value: str) -> None:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _toggle_pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        self._set_property(self.state_label, "pulse", "on" if self._pulse_on else "off")

    def clips_folder(self) -> Path | None:
        if self.status.final_clips:
            return self.status.final_clips[0].parent
        return self.status.session_dir


class EmptyState(QWidget):
    """Três passos, sem ícones genéricos: números grandes em acento e texto direto."""

    STEPS = (
        ("1", "Cole o link", "Do canal na Twitch, Kick ou YouTube. Pode ser antes de a live começar."),
        ("2", "O app espera e grava", "Quando o canal entra no ar, ele grava o vídeo e acompanha o chat."),
        ("3", "Os melhores momentos viram clipes",
         "Onde o chat explode, o app corta um vídeo vertical e coloca a legenda."),
    )

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 48, 0, 0)
        title = QLabel("Nenhuma live por aqui ainda.")
        title.setObjectName("emptyTitle")
        outer.addWidget(title)
        outer.addSpacing(28)
        row = QHBoxLayout()
        row.setSpacing(40)
        for number, heading, text in self.STEPS:
            column = QVBoxLayout()
            column.setSpacing(6)
            num = QLabel(number)
            num.setObjectName("stepNumber")
            head = QLabel(heading)
            head.setObjectName("stepHeading")
            head.setWordWrap(True)
            body = QLabel(text)
            body.setObjectName("message")
            body.setWordWrap(True)
            for widget in (num, head, body):
                column.addWidget(widget)
            column.addStretch(1)
            row.addLayout(column, 1)
        outer.addLayout(row)
        outer.addStretch(1)


class SettingsDialog(QDialog):
    """Ajustes simples. `values()` devolve o dict no formato do settings.json.

    `youtube_client` (injeção para testes) é o uploader.YouTubeClient usado na seção YouTube;
    sem ele, cria um cliente padrão."""

    def __init__(self, settings: dict, parent: QWidget | None = None, *,
                 youtube_client: object | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ajustes")
        self.setMinimumWidth(480)
        self._yt_client = youtube_client if youtube_client is not None else uploader.YouTubeClient()
        self._login_thread: threading.Thread | None = None
        form = QFormLayout()
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.layout_combo = self._combo(LAYOUT_CHOICES, settings["output_layout"])
        self.language_combo = self._combo(LANGUAGE_CHOICES, settings["whisper_language"])
        self.sensitivity_combo = self._combo(SENSITIVITY_CHOICES, settings["spike_multiplier"])
        self.dir_edit = QLineEdit(str(settings["sessions_dir"]))
        browse = QPushButton("Escolher…")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)

        form.addRow(self._section_title("Clipes"))
        form.addRow("Formato do clipe", self.layout_combo)
        form.addRow("Idioma da legenda", self.language_combo)
        form.addRow("Sensibilidade dos picos", self.sensitivity_combo)
        form.addRow("Pasta de saída", dir_row)
        hint = QLabel("Sensibilidade alta gera mais clipes, com momentos menos fortes.")
        hint.setObjectName("message")
        hint.setWordWrap(True)
        form.addRow("", hint)

        form.addRow(self._section_title("Google Drive"))
        current_export = str(settings.get("export_dir") or config.EXPORT_AUTO)
        self.export_edit = QLineEdit("" if current_export == config.EXPORT_AUTO else current_export)
        self.export_edit.setPlaceholderText("Automático")
        export_browse = QPushButton("Escolher…")
        export_browse.setObjectName("ghost")
        export_browse.clicked.connect(self._browse_export)
        export_detect = QPushButton("Detectar")
        export_detect.setObjectName("ghost")
        export_detect.clicked.connect(self._detect_export)
        export_row = QHBoxLayout()
        export_row.addWidget(self.export_edit, 1)
        export_row.addWidget(export_browse)
        export_row.addWidget(export_detect)
        form.addRow("Pasta do Google Drive", export_row)
        self.export_label = QLabel()
        self.export_label.setObjectName("message")
        self.export_label.setWordWrap(True)
        form.addRow("", self.export_label)
        self.export_edit.textChanged.connect(self._refresh_export_label)
        self._refresh_export_label()

        form.addRow(self._section_title("YouTube"))
        self.youtube_status_label = QLabel()
        self.connect_button = QPushButton("Conectar…")
        self.connect_button.clicked.connect(self._connect_youtube)
        self.disconnect_button = QPushButton("Desconectar")
        self.disconnect_button.setObjectName("ghost")
        self.disconnect_button.clicked.connect(self._disconnect_youtube)
        status_row = QHBoxLayout()
        status_row.addWidget(self.youtube_status_label, 1)
        status_row.addWidget(self.connect_button)
        status_row.addWidget(self.disconnect_button)
        form.addRow("Conta do YouTube", status_row)
        self.youtube_message_label = QLabel()
        self.youtube_message_label.setObjectName("message")
        self.youtube_message_label.setWordWrap(True)
        self.youtube_message_label.setVisible(False)
        form.addRow("", self.youtube_message_label)
        self.youtube_privacy_combo = self._combo(YOUTUBE_PRIVACY_CHOICES, settings["youtube_privacy"])
        form.addRow("Visibilidade", self.youtube_privacy_combo)
        self.youtube_auto_check = QCheckBox("Postar automaticamente cada clipe novo")
        self.youtube_auto_check.setChecked(bool(settings["youtube_auto_upload"]))
        form.addRow("", self.youtube_auto_check)
        self.youtube_help_button = QPushButton("Como criar a credencial")
        self.youtube_help_button.setObjectName("ghost")
        self.youtube_help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.youtube_help_button.clicked.connect(open_readme_section)
        form.addRow("", self.youtube_help_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Salvar")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._login_bridge = LoginBridge()
        self._login_bridge.finished.connect(self._on_login_finished)
        self.refresh_youtube_status()

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("sectionTitle")
        return label

    @staticmethod
    def _combo(choices, current) -> QComboBox:
        combo = QComboBox()
        for label, value in choices:
            combo.addItem(label, value)
        index = max(0, combo.findData(current))
        combo.setCurrentIndex(index)
        return combo

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Pasta de saída", self.dir_edit.text())
        if folder:
            self.dir_edit.setText(folder)

    def _browse_export(self) -> None:
        start = self.export_edit.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Pasta do Google Drive", start)
        if folder:
            self.export_edit.setText(folder)

    def _detect_export(self) -> None:
        self.export_edit.setText("")
        self._refresh_export_label()

    def _refresh_export_label(self) -> None:
        text = self.export_edit.text().strip()
        resolved = exporter.resolve_export_dir(text or config.EXPORT_AUTO)
        if not text or text == config.EXPORT_AUTO:
            if resolved is not None:
                self.export_label.setText(f"Detectado: {resolved}")
            else:
                self.export_label.setText(
                    "Google Drive para desktop não encontrado. Instale-o (google.com/drive/download) "
                    "ou escolha outra pasta.")
        else:
            self.export_label.setText(f"Os clipes vão para: {resolved}")

    # --- YouTube ---
    def refresh_youtube_status(self) -> None:
        connected = self._yt_client.is_connected()
        if connected:
            text = "Conectado"
        elif not self._yt_client.is_configured():
            text = "Falta o arquivo de credencial"
        else:
            text = "Não conectado"
        self.youtube_status_label.setText(text)
        logging_in = self._login_thread is not None and self._login_thread.is_alive()
        self.connect_button.setVisible(not connected)
        self.connect_button.setEnabled(not logging_in)
        self.disconnect_button.setVisible(connected)

    def _set_youtube_message(self, text: str) -> None:
        self.youtube_message_label.setText(text)
        self.youtube_message_label.setVisible(bool(text))

    def _connect_youtube(self) -> None:
        if self._login_thread is not None and self._login_thread.is_alive():
            return
        self._set_youtube_message("")
        if not self._yt_client.is_configured():
            path, _filter = QFileDialog.getOpenFileName(
                self, "Escolha o arquivo de credencial (.json) baixado do Google Cloud",
                str(Path.home() / "Downloads"), "Credencial do Google (*.json)")
            if not path:
                return
            try:
                self._yt_client.install_client_secret(Path(path))
            except uploader.UploadError as exc:       # AuthError é um UploadError
                self._set_youtube_message(str(exc))
                self.refresh_youtube_status()
                return
        self._set_youtube_message("Abrimos o navegador: entre na sua conta do Google e autorize "
                                  "o Auto-Clipper. Esta janela espera até você terminar.")
        client, bridge = self._yt_client, self._login_bridge

        def work() -> None:
            try:
                client.connect()
                result = ""
            except uploader.UploadError as exc:
                result = str(exc) or "Não deu para concluir o login."
            except Exception as exc:  # noqa: BLE001 - a thread nunca morre calada
                logger.exception("Erro inesperado no login do YouTube")
                result = f"Erro inesperado no login: {exc}"
            try:
                bridge.finished.emit(result)
            except RuntimeError:      # janela já destruída
                logger.debug("Ajustes fechados antes do fim do login")

        self._login_thread = threading.Thread(target=work, name="gui-yt-login", daemon=True)
        self._login_thread.start()
        self.refresh_youtube_status()

    def _on_login_finished(self, error: str) -> None:
        self._login_thread = None
        if error:
            self._set_youtube_message(error)
        else:
            self._set_youtube_message("Conta conectada. Os clipes podem ser postados pelo botão Enviar.")
        self.refresh_youtube_status()

    def _disconnect_youtube(self) -> None:
        try:
            self._yt_client.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao desconectar o YouTube")
        self._set_youtube_message("")
        self.refresh_youtube_status()

    def wait_login(self, timeout_s: float = 5.0) -> None:
        if self._login_thread is not None:
            self._login_thread.join(timeout_s)

    def values(self) -> dict:
        return {
            "output_layout": self.layout_combo.currentData(),
            "whisper_language": self.language_combo.currentData(),
            "spike_multiplier": self.sensitivity_combo.currentData(),
            "sessions_dir": self.dir_edit.text().strip() or default_settings()["sessions_dir"],
            "export_dir": self.export_edit.text().strip() or config.EXPORT_AUTO,
            "youtube_privacy": self.youtube_privacy_combo.currentData(),
            "youtube_auto_upload": self.youtube_auto_check.isChecked(),
        }


class ExportDialog(QDialog):
    """Escolha de clipes para copiar à pasta do Google Drive (ADR-011)."""

    def __init__(self, clips: list[Path], dest_dir: Path, channel: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clips = [Path(c) for c in clips]
        self.dest_dir = Path(dest_dir)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False
        self.setWindowTitle("Enviar ao Drive")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)
        title = QLabel(f"Enviar clipes de {channel}")
        title.setObjectName("channel")
        layout.addWidget(title)
        self.dest_label = QLabel(f"Destino: {self.dest_dir}")
        self.dest_label.setObjectName("message")
        self.dest_label.setWordWrap(True)
        layout.addWidget(self.dest_label)

        self.list = QListWidget()
        self.list.setMinimumHeight(220)
        self.list.itemChanged.connect(lambda _: self._update_selection())
        self.list.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.list, 1)

        tools = QHBoxLayout()
        self.select_all_button = QPushButton("Marcar todos")
        self.select_none_button = QPushButton("Desmarcar todos")
        for button in (self.select_all_button, self.select_none_button):
            button.setObjectName("ghost")
            tools.addWidget(button)
        self.select_all_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        self.select_none_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        tools.addStretch(1)
        self.count_label = QLabel()
        self.count_label.setObjectName("message")
        tools.addWidget(self.count_label)
        layout.addLayout(tools)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status_label = QLabel()
        self.status_label.setObjectName("message")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.open_folder_button = QPushButton("Abrir pasta")
        self.open_folder_button.setObjectName("ghost")
        self.open_folder_button.setVisible(False)
        self.open_folder_button.clicked.connect(lambda: open_folder(self.dest_dir))
        self.send_button = QPushButton("Enviar")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.start_export)
        self.close_button = QPushButton("Fechar")
        self.close_button.setObjectName("ghost")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.open_folder_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)

        self._bridge = ExportBridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.finished.connect(self._on_finished)
        self._bridge.failed.connect(self._on_failed)

        self._fill_list()

    # --- lista ---
    @staticmethod
    def _size_of(clip: Path) -> int:
        try:
            return clip.stat().st_size
        except OSError:
            return 0

    def _fill_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for clip in self.clips:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(clip))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self.list.addItem(item)
            self._refresh_item(item)
        self.list.blockSignals(False)
        self._update_selection()

    def _refresh_item(self, item: QListWidgetItem) -> None:
        clip = Path(item.data(Qt.ItemDataRole.UserRole))
        text = f"{clip.name}  ·  {format_size(self._size_of(clip))}"
        exported = exporter.is_already_exported(clip, self.dest_dir)
        if exported:
            text += "  ·  já está no Drive"
        item.setText(text)
        item.setCheckState(Qt.CheckState.Unchecked if exported else Qt.CheckState.Checked)

    def _items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())]

    def selected_clips(self) -> list[Path]:
        return [Path(it.data(Qt.ItemDataRole.UserRole)) for it in self._items()
                if it.checkState() == Qt.CheckState.Checked]

    def _set_all(self, state: Qt.CheckState) -> None:
        self.list.blockSignals(True)
        for item in self._items():
            item.setCheckState(state)
        self.list.blockSignals(False)
        self._update_selection()

    def _update_selection(self) -> None:
        selected = self.selected_clips()
        total = sum(self._size_of(c) for c in selected)
        self.count_label.setText(f"{len(selected)} selecionados ({format_size(total)})")
        n = len(selected)
        self.send_button.setText("Nenhum selecionado" if n == 0
                                 else "Enviar 1 clipe" if n == 1 else f"Enviar {n} clipes")
        self.send_button.setEnabled(n > 0 and not self._busy)

    def _open_item(self, item: QListWidgetItem) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.data(Qt.ItemDataRole.UserRole))))

    # --- envio ---
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (self.list, self.select_all_button, self.select_none_button):
            widget.setEnabled(not busy)
        self.progress.setVisible(busy)
        self._update_selection()

    def start_export(self) -> None:
        clips = self.selected_clips()
        if not clips or self._busy:
            return
        self._stop.clear()
        self.status_label.setVisible(False)
        self.open_folder_button.setVisible(False)
        self.progress.setRange(0, len(clips))
        self.progress.setValue(0)
        self._set_busy(True)
        bridge, stop, dest = self._bridge, self._stop, self.dest_dir

        def emit(signal, *args) -> None:
            try:
                signal.emit(*args)
            except RuntimeError:       # janela já destruída
                logger.debug("Janela de envio fechada antes do fim da cópia")

        def work() -> None:
            try:
                result = exporter.export_clips(
                    clips, dest, stop_event=stop,
                    on_progress=lambda done, total: emit(bridge.progress, done, total))
            except (exporter.ExportError, OSError) as exc:
                logger.exception("Falha ao enviar clipes para %s", dest)
                emit(bridge.failed, str(exc))
            except Exception as exc:  # noqa: BLE001 - a thread nunca morre calada
                logger.exception("Erro inesperado ao enviar clipes para %s", dest)
                emit(bridge.failed, str(exc))
            else:
                emit(bridge.finished, result)

        self._thread = threading.Thread(target=work, name="gui-export", daemon=True)
        self._thread.start()

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _on_finished(self, result) -> None:
        self._set_busy(False)
        self._fill_list()
        copied, skipped = len(result.copied), len(result.skipped)
        text = (f"{copied} clipe copiado para o Drive." if copied == 1
                else f"{copied} clipes copiados para o Drive.")
        text += " O Google Drive envia para a nuvem em segundo plano."
        if skipped:
            text += f" {skipped} já estava lá." if skipped == 1 else f" {skipped} já estavam lá."
        self.status_label.setText(text)
        self.status_label.setVisible(True)
        self.open_folder_button.setVisible(True)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._fill_list()
        self.status_label.setText(
            f"Não deu para copiar os clipes: {message}. Confira se a pasta de destino está acessível "
            "e se há espaço livre.")
        self.status_label.setVisible(True)

    def wait_finished(self, timeout_s: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout_s)

    def closeEvent(self, event) -> None:  # noqa: N802 (nome do Qt)
        self._stop.set()
        super().closeEvent(event)

    def reject(self) -> None:
        self._stop.set()
        super().reject()


class YouTubeDialog(QDialog):
    """Escolha de clipes para postar no YouTube (ADR-012).

    Não envia nada sozinha: entrega cada clipe à `UploadQueue` compartilhada da janela
    principal e mostra o andamento pelos eventos que a janela repassa a `handle_event`.
    Fechar a janela NÃO cancela a fila: os envios seguem em segundo plano."""

    settings_requested = Signal()
    COL_CHECK, COL_CLIP, COL_TITLE, COL_STATE = range(4)

    def __init__(self, clips: list[Path], channel: str, platform: str, queue: object | None, *,
                 session_dir: Path | None = None, connected: bool = True,
                 privacy: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clips = [Path(c) for c in clips]
        self.channel = channel
        self.platform = platform
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self._queue = queue
        self._rows: dict[Path, int] = {}
        self._requested: dict[Path, str] = {}
        self._posted_urls: dict[int, str] = {}
        self._locked: set[int] = set()
        self.setWindowTitle(f"Postar clipes de {channel} no YouTube")
        self.setMinimumWidth(720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)
        title = QLabel(f"Postar clipes de {channel} no YouTube")
        title.setObjectName("channel")
        layout.addWidget(title)

        self.not_connected_label = QLabel(
            "O YouTube ainda não está conectado. Conecte a sua conta em Ajustes para postar os clipes.")
        self.not_connected_label.setObjectName("message")
        self.not_connected_label.setWordWrap(True)
        self.settings_button = QPushButton("Abrir Ajustes")
        self.settings_button.setObjectName("primary")
        self.settings_button.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.not_connected_label)
        layout.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Clipe", "Título", "Estado"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(240)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_CLIP, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_STATE, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(lambda _item: self._update_selection())
        self.table.cellDoubleClicked.connect(self._open_cell)
        layout.addWidget(self.table, 1)

        tools = QHBoxLayout()
        self.select_all_button = QPushButton("Marcar todos")
        self.select_none_button = QPushButton("Desmarcar todos")
        for button in (self.select_all_button, self.select_none_button):
            button.setObjectName("ghost")
            tools.addWidget(button)
        self.select_all_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        self.select_none_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        tools.addStretch(1)
        self.privacy_combo = QComboBox()
        for label, value in YOUTUBE_PRIVACY_CHOICES:
            self.privacy_combo.addItem(label, value)
        self.privacy_combo.setCurrentIndex(
            max(0, self.privacy_combo.findData(privacy or config.YOUTUBE_PRIVACY)))
        tools.addWidget(QLabel("Visibilidade"))
        tools.addWidget(self.privacy_combo)
        self.count_label = QLabel()
        self.count_label.setObjectName("message")
        tools.addWidget(self.count_label)
        layout.addLayout(tools)

        self.status_label = QLabel()
        self.status_label.setObjectName("message")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)
        self.warning_label = QLabel()
        self.warning_label.setObjectName("formError")
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label)
        self.footer_label = QLabel("Pode fechar esta janela: os envios continuam em segundo plano "
                                   "e o cartão da live mostra o andamento.")
        self.footer_label.setObjectName("message")
        self.footer_label.setWordWrap(True)
        layout.addWidget(self.footer_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton("Fechar")
        self.close_button.setObjectName("ghost")
        self.close_button.clicked.connect(self.close)
        self.send_button = QPushButton("Nenhum selecionado")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.start_upload)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)

        self._connected = bool(connected) and queue is not None
        for widget in (self.table, self.select_all_button, self.select_none_button,
                       self.privacy_combo, self.count_label, self.send_button, self.footer_label):
            widget.setVisible(self._connected)
        self.not_connected_label.setVisible(not self._connected)
        self.settings_button.setVisible(not self._connected)
        if self._connected:
            self._fill_table()

    # --- tabela ---
    @staticmethod
    def _size_of(clip: Path) -> int:
        try:
            return clip.stat().st_size
        except OSError:
            return 0

    def _fill_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.clips))
        for row, clip in enumerate(self.clips):
            self._rows[clip] = row
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            name = QTableWidgetItem(f"{clip.name}  ·  {format_size(self._size_of(clip))}")
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            name.setData(Qt.ItemDataRole.UserRole, str(clip))
            title = QTableWidgetItem()
            state = QTableWidgetItem()
            state.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for col, item in enumerate((check, name, title, state)):
                self.table.setItem(row, col, item)
            record = uploader.is_posted(clip, self.session_dir)
            if record is not None:
                self._mark_posted(row, record)
            else:
                meta = uploader.build_meta(clip, channel=self.channel, platform=self.platform,
                                           privacy=self.privacy_combo.currentData())
                title.setText(meta.title)
                title.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                               | Qt.ItemFlag.ItemIsEditable)
                check.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_selection()

    def _mark_posted(self, row: int, record: uploader.UploadRecord) -> None:
        """Linha já postada: desmarcada, não editável, com o link no lugar do título."""
        check, name, title, state = (self.table.item(row, c) for c in range(4))
        check.setFlags(Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Unchecked)
        title.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        title.setText(f"postado · {record.url}")
        state.setText("postado")
        self._posted_urls[row] = record.url
        self._locked.discard(row)

    def _set_row_locked(self, row: int, locked: bool) -> None:
        """Enquanto está na fila/enviando, a linha não pode ser marcada nem editada."""
        check, _name, title, _state = (self.table.item(row, c) for c in range(4))
        check.setFlags(Qt.ItemFlag.ItemIsEnabled if locked
                       else Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        title.setFlags(base if locked else base | Qt.ItemFlag.ItemIsEditable)
        (self._locked.add if locked else self._locked.discard)(row)

    def _set_state(self, clip: Path, text: str) -> None:
        row = self._rows.get(clip)
        if row is not None:
            self.table.blockSignals(True)
            self.table.item(row, self.COL_STATE).setText(text)
            self.table.blockSignals(False)

    def _checked_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount())
                if r not in self._posted_urls and r not in self._locked
                and self.table.item(r, self.COL_CHECK).checkState() == Qt.CheckState.Checked]

    def selected_clips(self) -> list[Path]:
        return [self.clips[r] for r in self._checked_rows()]

    def titles(self) -> dict[Path, str]:
        return {self.clips[r]: self.table.item(r, self.COL_TITLE).text() for r in self._checked_rows()}

    def _set_all(self, state: Qt.CheckState) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if row not in self._posted_urls and row not in self._locked:
                self.table.item(row, self.COL_CHECK).setCheckState(state)
        self.table.blockSignals(False)
        self._update_selection()

    def _update_selection(self) -> None:
        selected = self.selected_clips()
        n = len(selected)
        total = sum(self._size_of(c) for c in selected)
        self.count_label.setText(f"{n} selecionados ({format_size(total)})")
        self.send_button.setText("Nenhum selecionado" if n == 0
                                 else "Postar 1 clipe" if n == 1 else f"Postar {n} clipes")
        self.send_button.setEnabled(n > 0)

    def _open_cell(self, row: int, column: int) -> None:
        url = self._posted_urls.get(row)
        if url is not None:
            QDesktopServices.openUrl(QUrl(url))
        elif column == self.COL_CLIP:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.clips[row])))

    # --- envio ---
    def start_upload(self) -> None:
        """Entrega à fila os clipes marcados, com o título editado e a visibilidade do combo."""
        if not self._connected or self._queue is None:
            return
        privacy = self.privacy_combo.currentData()
        titles = self.titles()
        self.warning_label.setVisible(False)
        self.table.blockSignals(True)
        for clip, title in titles.items():
            row = self._rows[clip]
            meta = uploader.build_meta(clip, channel=self.channel, platform=self.platform,
                                       privacy=privacy, title=title)
            self._requested[clip] = privacy
            accepted = self._queue.submit(clip, meta, self.session_dir)
            if accepted:
                self.table.item(row, self.COL_STATE).setText("na fila")
                self.table.item(row, self.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
                self._set_row_locked(row, True)
            else:
                record = uploader.is_posted(clip, self.session_dir)
                if record is not None:
                    self._mark_posted(row, record)
                else:
                    self.table.item(row, self.COL_STATE).setText("já está na fila")
                    self.table.item(row, self.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selection()
        sent = len(titles)
        self.status_label.setText(f"{sent} clipe enviado para a fila." if sent == 1
                                  else f"{sent} clipes enviados para a fila.")
        self.status_label.setVisible(sent > 0)

    def handle_event(self, event: uploader.UploadEvent) -> None:
        """Repassado pela janela principal (thread da UI) para cada UploadEvent da fila."""
        row = self._rows.get(Path(event.clip))
        if row is None or not self._connected:
            return
        clip = Path(event.clip)
        if event.kind == "queued":
            self._set_state(clip, "na fila")
        elif event.kind == "started":
            self._set_state(clip, "enviando 0%")
        elif event.kind == "progress":
            pct = int(event.done_bytes * 100 / event.total_bytes) if event.total_bytes else 0
            self._set_state(clip, f"enviando {pct}%")
        elif event.kind == "done" and event.record is not None:
            self.table.blockSignals(True)
            self._mark_posted(row, event.record)
            self.table.blockSignals(False)
            self._update_selection()
            requested = self._requested.get(clip)
            if event.record.privacy == "private" and requested not in (None, "private"):
                self.warning_label.setText(YOUTUBE_PRIVATE_WARNING)
                self.warning_label.setVisible(True)
        elif event.kind == "failed":
            self._set_state(clip, f"falhou: {event.error or 'erro desconhecido'}")
            self.table.blockSignals(True)
            self._set_row_locked(row, False)
            self.table.blockSignals(False)
            self._update_selection()


# ───────────────────────────────── janela principal ──────────────────────────
DISK_JOB_PREFIX = "disk:"      # ids de cartões abertos do disco: o motor nunca os vê


def load_recorded_session(path: Path) -> JobStatus:
    """Monta um JobStatus (DONE) de uma live já gravada, só lendo o disco (sem reprocessar).

    Aceita a pasta da sessão (com session.json) ou a pasta final/ dentro dela.
    Levanta ValueError com mensagem pt-BR se a pasta não servir.
    """
    path = Path(path)
    if (path / "session.json").is_file():
        root = path
    elif path.name.lower() == "final" and path.is_dir():
        root = path.parent
    elif (path / "final").is_dir():
        root = path
    else:
        raise ValueError("Essa pasta não parece ser de uma live gravada pelo Auto-Clipper.")
    root = root.resolve()
    clips = tuple(sorted(p for p in (root / "final").glob("*.mp4") if p.is_file()))         if (root / "final").is_dir() else ()
    if not clips:
        raise ValueError("Essa gravação ainda não tem clipes prontos na pasta final.")

    url, platform, channel = "", "", root.name
    recorded_s, recorded_bytes = 0.0, 0
    try:
        session = Session.load(root)
        url, platform, channel = session.url, session.platform, session.channel
        for seg in session.segments:
            if seg.wall_start is not None and seg.wall_end is not None:
                recorded_s += max(0.0, seg.wall_end - seg.wall_start)
            try:
                recorded_bytes += seg.path.stat().st_size
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - manifesto ausente/antigo: cai no nome da pasta
        parts = root.name.split("_", 2)
        if len(parts) == 3 and parts[2]:
            platform, channel = parts[1], parts[2]
    return JobStatus(
        job_id=DISK_JOB_PREFIX + str(root), url=url, platform=platform, channel=channel,
        state=JobState.DONE, message="Gravação aberta do disco",
        recorded_s=recorded_s, recorded_bytes=recorded_bytes, spikes=len(clips),
        clips_done=len(clips), clips_total=len(clips), session_dir=root, final_clips=clips,
        updated_at=time.time())


class MainWindow(QWidget):
    def __init__(self, manager_factory: Callable[[Callable[[JobStatus], None]], object],
                 settings_path: Path = SETTINGS_PATH,
                 hardware_probe: Callable[[], str] = detect_hardware_text,
                 check_ffmpeg: Callable[[], bool] = ffmpeg_available, *,
                 youtube_client_factory: Callable[[], object] = uploader.YouTubeClient,
                 upload_queue_factory: Callable[[object, Callable[[object], None]], object] = uploader.UploadQueue,
                 ) -> None:
        super().__init__()
        self.setWindowTitle("Auto-Clipper")
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self._settings_path = Path(settings_path)
        self._settings = load_settings(self._settings_path)
        apply_settings(self._settings)
        self._cards: dict[str, JobCard] = {}

        # YouTube (ADR-012): cliente e fila são criados só quando alguém precisa deles.
        self._youtube_client_factory = youtube_client_factory
        self._upload_queue_factory = upload_queue_factory
        self._yt_client: object | None = None
        self._yt_queue: object | None = None
        self._yt_bridge = YouTubeBridge()
        self._yt_bridge.event.connect(self._on_upload_event)
        self._youtube_dialog: YouTubeDialog | None = None
        self._clip_jobs: dict[Path, str] = {}                  # clipe -> job_id (para achar o cartão)
        self._clip_states: dict[str, dict[Path, str]] = {}     # job_id -> clipe -> estado do envio
        # Auto-post: guarda TODO clipe já visto, com a opção ligada ou não. Assim, ligar a opção
        # depois só posta os clipes que surgirem a partir dali (nada retroativo, nem das lives
        # que já estavam na tela quando o app abriu).
        self._seen_clips: set[Path] = set()
        self._auto_post_ready = False          # só vale depois do snapshot inicial das lives
        self._auto_post_blocked = False        # login expirou: pausa o auto-post até reconectar
        self._notified: set[str] = set()       # avisos de auth/cota já mostrados

        self._bridge = UpdateBridge()
        self._bridge.updated.connect(self._on_status)
        self._build(check_ffmpeg())
        self.manager = manager_factory(self._bridge.updated.emit)
        self._start_hardware_probe(hardware_probe)
        self._disk_timer = QTimer(self)
        self._disk_timer.timeout.connect(self.refresh_footer)
        self._disk_timer.timeout.connect(ensure_app_log)
        self._disk_timer.start(DISK_REFRESH_MS)
        for status in self.manager.all():       # lives que já existiam (ex.: demo)
            self._on_status(status)
        self._auto_post_ready = True
        self._refresh_visibility()
        self.refresh_footer()

    # --- construção ---
    def _build(self, ffmpeg_ok: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 0)
        root.setSpacing(0)

        top = QHBoxLayout()
        title = QLabel("Auto-Clipper")
        title.setObjectName("appTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.open_recording_button = QPushButton("Abrir gravação")
        self.open_recording_button.setObjectName("ghost")
        self.open_recording_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_recording_button.clicked.connect(self.open_recording)
        top.addWidget(self.open_recording_button)
        self.settings_button = QPushButton("Ajustes")
        self.settings_button.setObjectName("ghost")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self.open_settings)
        top.addWidget(self.settings_button)
        root.addLayout(top)
        root.addSpacing(20)

        self.ffmpeg_banner = QLabel(
            f"O FFmpeg não foi encontrado, e sem ele não dá para gerar clipes. "
            f"Abra o PowerShell e rode:  {FFMPEG_INSTALL_CMD}  (depois feche e abra o Auto-Clipper).")
        self.ffmpeg_banner.setObjectName("banner")
        self.ffmpeg_banner.setWordWrap(True)
        self.ffmpeg_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.ffmpeg_banner.setVisible(not ffmpeg_ok)
        root.addWidget(self.ffmpeg_banner)
        if not ffmpeg_ok:
            root.addSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlEdit")
        self.url_edit.setPlaceholderText(f"Cole aqui o link da live (Twitch, Kick ou YouTube), ex.: {EXAMPLE_URL}")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.returnPressed.connect(self.add_live)
        self.url_edit.textChanged.connect(lambda _: self.set_error(""))
        self.add_button = QPushButton("Adicionar live")
        self.add_button.setObjectName("primary")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self.add_live)
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.add_button)
        root.addLayout(row)

        self.error_label = QLabel()
        self.error_label.setObjectName("formError")
        self.error_label.setVisible(False)
        root.addSpacing(6)
        root.addWidget(self.error_label)
        root.addSpacing(10)

        self.empty_state = EmptyState()
        root.addWidget(self.empty_state, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self.cards_layout = QVBoxLayout(holder)
        self.cards_layout.setContentsMargins(0, 0, 8, 0)
        self.cards_layout.setSpacing(12)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(0, 10, 0, 10)
        foot.setSpacing(28)
        self.hardware_label = QLabel("Detectando placa de vídeo…")
        self.disk_label = QLabel()
        self.lives_label = QLabel()
        self.notice_label = QLabel()          # avisos do YouTube (login expirado, cota)
        self.notice_label.setVisible(False)
        for label in (self.hardware_label, self.disk_label):
            foot.addWidget(label)
        foot.addWidget(self.notice_label)
        foot.addStretch(1)
        foot.addWidget(self.lives_label)
        root.addSpacing(8)
        root.addWidget(footer)

    def _start_hardware_probe(self, probe: Callable[[], str]) -> None:
        """hardware.detect() faz encodes de teste: roda numa thread para não travar a UI."""
        self._hardware_bridge = HardwareBridge()
        self._hardware_bridge.detected.connect(
            lambda text: self.hardware_label.setText(f"Encoder: {text}"))

        def work() -> None:
            try:
                text = probe()
            except Exception:  # noqa: BLE001
                text = "CPU"
            self._hardware_bridge.detected.emit(text)

        threading.Thread(target=work, name="gui-hardware", daemon=True).start()

    # --- interação ---
    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    @staticmethod
    def normalize_url(raw: str) -> str:
        raw = raw.strip()
        if raw and "://" not in raw:
            raw = "https://" + raw
        return raw

    def add_live(self) -> None:
        raw = self.url_edit.text().strip()
        if not raw:
            self.set_error("Cole o link da live no campo acima.")
            return
        url = self.normalize_url(raw)
        try:
            detect_platform(url)
            channel_from_url(url)
        except ValueError:
            self.set_error("Esse link não parece ser de uma live da Twitch, Kick ou YouTube.")
            return
        try:
            job_id = self.manager.add(url, once=True)
        except (ValueError, RuntimeError) as exc:
            self.set_error(str(exc))
            return
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao adicionar a live %s", url)
            self.set_error("Não deu para adicionar essa live. Veja o log para detalhes.")
            return
        self.url_edit.clear()
        self.set_error("")
        if job_id not in self._cards:
            try:
                self._on_status(self.manager.status(job_id))
            except Exception:  # noqa: BLE001 - o signal do motor cria o cartão
                logger.debug("status inicial indisponível para %s", job_id)

    def confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.button(QMessageBox.StandardButton.Yes).setText("Sim")
        box.button(QMessageBox.StandardButton.No).setText("Não")
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _sessions_start_dir(self) -> Path:
        base = Path(str(self._settings["sessions_dir"]))
        if not base.is_absolute() and not base.exists():
            base = Path(runtime.default_sessions_dir())
        return base if base.exists() else Path.home()

    def open_recording(self) -> None:
        """Carrega uma live já gravada como cartão (só mostra o que existe em final/)."""
        folder = QFileDialog.getExistingDirectory(
            self, "Escolha a pasta da live gravada", str(self._sessions_start_dir()))
        if not folder:
            return
        try:
            status = load_recorded_session(Path(folder))
        except ValueError as exc:
            self.set_error(str(exc))
            return
        self.set_error("")
        for card in self._cards.values():
            other = card.status.session_dir
            if other is not None and Path(other).resolve() == status.session_dir:
                self.scroll.ensureWidgetVisible(card)
                return
        self._on_status(status)
        self.scroll.ensureWidgetVisible(self._cards[status.job_id])

    def _on_status(self, status: JobStatus) -> None:
        card = self._cards.get(status.job_id)
        if card is None:
            card = JobCard(status)
            card.stop_requested.connect(self._stop_job)
            card.open_requested.connect(self._open_clips)
            card.export_requested.connect(self._export_clips)
            card.youtube_requested.connect(self._youtube_clips)
            card.remove_requested.connect(self._remove_job)
            self._cards[status.job_id] = card
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        else:
            card.update_status(status)
        for clip in status.final_clips:
            self._clip_jobs[clip] = status.job_id
        if status.job_id.startswith(DISK_JOB_PREFIX):
            self._seen_clips.update(status.final_clips)    # clipes antigos: nunca postam sozinhos
        else:
            self._auto_post(status)
        self._refresh_visibility()
        self.refresh_footer()

    def _stop_job(self, job_id: str) -> None:
        card = self._cards[job_id]
        if self.confirm("Parar esta live?",
                        f"Parar {card.status.channel}? O que já foi gravado vira clipes normalmente."):
            self.manager.stop(job_id)

    def _open_clips(self, job_id: str) -> None:
        folder = self._cards[job_id].clips_folder()
        if folder is not None:
            open_folder(folder)

    def _export_clips(self, job_id: str) -> None:
        card = self._cards[job_id]
        base = exporter.resolve_export_dir(self._settings.get("export_dir"))
        if base is None:
            if self.confirm("Google Drive não encontrado",
                            "O Google Drive para desktop não foi encontrado neste computador. "
                            "Instale-o (google.com/drive/download) ou escolha outra pasta em Ajustes.\n\n"
                            "Abrir os Ajustes agora?"):
                self.open_settings()
            return
        clips = list(card.status.final_clips)
        dest = exporter.destination_for(base, card.status.session_dir, clips)
        self._export_dialog = ExportDialog(clips, dest, card.status.channel, self)
        self._export_dialog.exec()

    # --- YouTube ---
    def youtube_client(self):
        if self._yt_client is None:
            self._yt_client = self._youtube_client_factory()
        return self._yt_client

    def upload_queue(self):
        if self._yt_queue is None:
            self._yt_queue = self._upload_queue_factory(self.youtube_client(), self._yt_bridge.event.emit)
        return self._yt_queue

    def _youtube_clips(self, job_id: str) -> None:
        card = self._cards[job_id]
        connected = bool(self.youtube_client().is_connected())
        dialog = YouTubeDialog(
            list(card.status.final_clips), card.status.channel, card.status.platform,
            self.upload_queue() if connected else None, session_dir=card.status.session_dir,
            connected=connected, privacy=self._settings.get("youtube_privacy"), parent=self)
        dialog.settings_requested.connect(lambda: self._settings_from_youtube_dialog(dialog))
        self._youtube_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._youtube_dialog = None

    def _settings_from_youtube_dialog(self, dialog: QDialog) -> None:
        dialog.accept()
        QTimer.singleShot(0, self.open_settings)

    def _auto_post(self, status: JobStatus) -> None:
        """Posta sozinho os clipes NOVOS de `status.final_clips` (se a opção estiver ligada)."""
        fresh = [clip for clip in status.final_clips if clip not in self._seen_clips]
        if not fresh:
            return
        self._seen_clips.update(fresh)
        if (not self._auto_post_ready or not self._settings.get("youtube_auto_upload")
                or self._auto_post_blocked):
            return
        try:
            if not self.youtube_client().is_connected():
                return
            queue = self.upload_queue()
            for clip in fresh:
                meta = uploader.build_meta(clip, channel=status.channel, platform=status.platform,
                                           privacy=self._settings.get("youtube_privacy"))
                queue.submit(clip, meta, status.session_dir)
        except Exception:  # noqa: BLE001 - postar é opcional: nunca derruba a atualização da tela
            logger.exception("Falha na postagem automática no YouTube")

    def _on_upload_event(self, event: uploader.UploadEvent) -> None:
        clip = Path(event.clip)
        job_id = self._clip_jobs.get(clip)
        if job_id is not None:
            states = self._clip_states.setdefault(job_id, {})
            states[clip] = {"queued": "queued", "started": "active", "progress": "active",
                            "done": "done", "failed": "failed"}.get(event.kind, "queued")
            card = self._cards.get(job_id)
            if card is not None:
                card.set_youtube_text(self._youtube_card_text(states))
        if event.kind == "done":
            self._notified.clear()
            self.notice_label.setVisible(False)
        elif event.kind == "failed" and event.error_kind in ("auth", "quota"):
            self._notify_youtube_problem(event.error_kind, event.error or "")
        if self._youtube_dialog is not None:
            self._youtube_dialog.handle_event(event)

    @staticmethod
    def _youtube_card_text(states: dict[Path, str]) -> str:
        values = list(states.values())
        done, failed = values.count("done"), values.count("failed")
        waiting = values.count("queued") + values.count("active")
        if waiting:
            return f"YouTube: enviando {done + failed + 1} de {len(values)}"
        parts = []
        if done:
            parts.append(f"{done} postado" if done == 1 else f"{done} postados")
        if failed:
            parts.append(f"{failed} falhou" if failed == 1 else f"{failed} falharam")
        return "YouTube: " + ", ".join(parts) if parts else ""

    def _notify_youtube_problem(self, kind: str, message: str) -> None:
        """Aviso discreto no rodapé, uma vez por problema (nada de caixa de diálogo em loop)."""
        if kind == "auth":
            self._auto_post_blocked = True
        if kind in self._notified:
            return
        self._notified.add(kind)
        text = ("YouTube: o login expirou. Conecte de novo em Ajustes; a postagem automática "
                "ficou em pausa." if kind == "auth"
                else "YouTube: a cota diária de envios acabou. Tente de novo amanhã.")
        self.notice_label.setText(text)
        self._set_label_flag(self.notice_label, True)
        self.notice_label.setVisible(True)
        logger.warning("Aviso do YouTube (%s): %s", kind, message)

    def _remove_job(self, job_id: str) -> None:
        if not job_id.startswith(DISK_JOB_PREFIX):     # cartão de disco não existe no motor
            try:
                self.manager.remove(job_id)
            except ValueError as exc:      # live ainda ativa: o botão não deveria estar visível
                logger.warning("Não foi possível remover a live %s: %s", job_id, exc)
                return
        card = self._cards.pop(job_id, None)
        if card is not None:
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self._refresh_visibility()
        self.refresh_footer()

    def _refresh_visibility(self) -> None:
        has_cards = bool(self._cards)
        self.empty_state.setVisible(not has_cards)
        self.scroll.setVisible(has_cards)

    def active_count(self) -> int:
        return sum(card.status.state in ACTIVE_STATES for card in self._cards.values())

    def refresh_footer(self) -> None:
        free = free_disk_gb(self._settings["sessions_dir"])
        if free is None:
            self.disk_label.setText("Disco: indisponível")
            self._set_label_flag(self.disk_label, False)
        elif free < DISK_WARN_GB:
            self.disk_label.setText(f"! Pouco espaço: {free:.0f} GB livres")
            self._set_label_flag(self.disk_label, True)
        else:
            self.disk_label.setText(f"Disco livre: {free:.0f} GB")
            self._set_label_flag(self.disk_label, False)
        self.lives_label.setText(f"Lives ativas: {self.active_count()} de {config.MAX_PARALLEL_LIVES}")

    @staticmethod
    def _set_label_flag(label: QLabel, warn: bool) -> None:
        label.setProperty("warn", "true" if warn else "false")
        label.style().unpolish(label)
        label.style().polish(label)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self._settings, self, youtube_client=self.youtube_client())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.wait_login()
        if self._auto_post_blocked and self.youtube_client().is_connected():
            self._auto_post_blocked = False        # reconectou: o auto-post volta
            self._notified.discard("auth")
            self.notice_label.setVisible(False)
        if accepted:
            self.apply_new_settings(dialog.values())

    def apply_new_settings(self, values: dict) -> None:
        self._settings = values
        apply_settings(values)
        try:
            save_settings(values, self._settings_path)
        except OSError:
            logger.exception("Não foi possível salvar %s", self._settings_path)
        self.refresh_footer()

    # --- fechar ---
    def closeEvent(self, event) -> None:  # noqa: N802 (nome do Qt)
        uploading = self._yt_queue.pending() if self._yt_queue is not None else 0
        if (self.active_count() or uploading) and not self.confirm(
                "Sair do Auto-Clipper?",
                "Parar tudo e sair? O que já foi gravado fica salvo no disco."
                + (f" {uploading} envio(s) ao YouTube em andamento serão cancelados." if uploading else "")):
            event.ignore()
            return
        self._disk_timer.stop()
        self._shutdown_manager()
        event.accept()

    def _shutdown_manager(self) -> None:
        """manager.shutdown(wait=True) pode demorar: roda em thread, sem congelar a janela."""
        if self._yt_queue is not None:
            try:
                self._yt_queue.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("Falha ao encerrar a fila de envio ao YouTube")
        worker = threading.Thread(target=self.manager.shutdown, kwargs={"wait": True},
                                  name="gui-shutdown", daemon=True)
        worker.start()
        self.setWindowTitle("Encerrando o Auto-Clipper…")
        self.setEnabled(False)
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_S
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if not worker.is_alive() or time.monotonic() > deadline else None)
        poll.start(100)
        loop.exec()
        poll.stop()


# ───────────────────────────────────── tema ──────────────────────────────────
def build_stylesheet() -> str:
    display = ", ".join(f'"{f}"' for f in (FONT_DISPLAY, *FONT_FALLBACKS))
    body = ", ".join(f'"{f}"' for f in (FONT_BODY, "Arial"))
    return f"""
* {{ font-family: {body}; font-size: 13px; color: {FG}; }}
QWidget {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QToolTip {{ background: {SURFACE}; color: {FG}; border: 1px solid {BORDER}; }}
#appTitle {{ font-family: {display}; font-size: 30px; letter-spacing: 0.5px; }}
#emptyTitle {{ font-family: {display}; font-size: 22px; }}
#stepNumber {{ font-family: {display}; font-size: 56px; color: {FG_FAINT}; }}
#stepHeading {{ font-family: {display}; font-size: 18px; }}
#message {{ color: {FG_DIM}; }}
#channel {{ font-family: {display}; font-size: 24px; }}
#platformTag {{ color: {FG_FAINT}; font-size: 12px; padding-bottom: 4px; }}
#stateLabel {{ font-family: {display}; font-size: 16px; color: {FG_DIM}; }}
#statValue {{ font-family: {display}; font-size: 17px; }}
#statCaption {{ color: {FG_FAINT}; font-size: 11px; }}
#bigTime {{ font-family: {display}; font-size: 44px; }}
#footer {{ border-top: 1px solid {BORDER}; }}
#footer QLabel {{ color: {FG_FAINT}; font-size: 12px; }}
#footer QLabel[warn="true"] {{ color: {FG}; font-weight: bold; }}
#formError {{ color: {FG}; font-weight: 600; }}
#banner {{ border: 1px solid {ACCENT}; border-radius: {RADIUS_PX}px; padding: 12px 14px; color: {FG}; }}

QLineEdit {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS_PX}px;
            padding: 0 14px; min-height: 28px; selection-background-color: {ACCENT}; selection-color: {BG}; }}
QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS_PX}px; padding: 4px; }}
QListWidget::item {{ padding: 6px 4px; }}
QListWidget::item:hover {{ background: {SURFACE_HOVER}; }}
QListWidget:disabled {{ color: {FG_FAINT}; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
#urlEdit {{ min-height: 40px; font-size: 14px; }}

QPushButton {{ background: transparent; border: 1px solid {BORDER}; border-radius: {RADIUS_PX}px;
              padding: 7px 16px; color: {FG}; }}
QPushButton:hover {{ border-color: {FG_DIM}; background: {SURFACE_HOVER}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {FG_FAINT}; border-color: {SURFACE}; background: transparent; }}
QPushButton#primary, QPushButton[primary="true"] {{ background: {ACCENT}; color: {BG}; border: 1px solid {ACCENT};
              font-weight: 700; }}
QPushButton#primary {{ padding: 0 24px; min-height: 40px; font-size: 14px; }}
QPushButton#primary:hover, QPushButton[primary="true"]:hover {{ background: #d6ff3d; border-color: #d6ff3d; }}
QPushButton#primary:disabled {{ background: transparent; color: {FG_FAINT}; border: 1px solid {BORDER}; }}
QPushButton#ghost {{ border-color: transparent; color: {FG_DIM}; }}
QPushButton#ghost:hover {{ color: {FG}; background: {SURFACE}; }}

#jobCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-left: 3px solid {BORDER};
           border-radius: {RADIUS_PX}px; }}
#jobCard QLabel, #jobCard QWidget {{ background: transparent; }}
#jobCard QPushButton[primary="true"] {{ background: {ACCENT}; color: {BG}; border: 1px solid {ACCENT}; font-weight: 700; }}
#jobCard[state="waiting"] #bigTime, #jobCard[state="stopped"] #bigTime {{ color: {FG_DIM}; }}
#jobCard[state="recording"] {{ border-left: 3px solid {ACCENT}; }}
#jobCard[state="processing"] {{ border-left: 3px solid {FG_DIM}; }}
#jobCard[state="done"] {{ border: 1px solid {ACCENT}; border-left: 3px solid {ACCENT}; }}
#jobCard[state="error"] {{ border: 1px solid {FG_DIM}; border-left: 3px solid {FG}; }}
#jobCard[state="stopped"] #channel, #jobCard[state="waiting"] #channel {{ color: {FG_DIM}; }}
#stateLabel[pulse="on"] {{ color: {FG_DIM}; }}
#jobCard[state="recording"] #stateLabel[pulse="on"] {{ color: {ACCENT}; }}
#jobCard[state="recording"] #stateLabel[pulse="off"] {{ color: {ACCENT_DIM}; }}
#jobCard[state="done"] #stateLabel {{ color: {ACCENT}; }}
#jobCard[state="error"] #stateLabel, #jobCard[state="queued"] #stateLabel {{ color: {FG}; }}

QProgressBar {{ background: {BORDER}; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QScrollBar:vertical {{ background: {BG}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {FG_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

#sectionTitle {{ color: {FG_FAINT}; font-size: 11px; letter-spacing: 1px; padding-top: 8px; }}
QPushButton::menu-indicator {{ image: none; width: 0; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 7px 24px; background: transparent; color: {FG}; }}
QMenu::item:selected {{ background: {ACCENT}; color: {BG}; }}
QMenu::item:disabled {{ color: {FG_FAINT}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QTableView::indicator {{ width: 16px; height: 16px; border: 1px solid {BORDER};
            border-radius: 3px; background: {SURFACE}; }}
QCheckBox::indicator:hover, QTableView::indicator:hover {{ border-color: {FG_DIM}; }}
QCheckBox::indicator:checked, QTableView::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox:focus {{ color: {ACCENT}; }}
QTableWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS_PX}px;
            gridline-color: {BORDER}; selection-background-color: {SURFACE_HOVER}; selection-color: {FG}; }}
QTableWidget::item {{ padding: 6px 4px; }}
QTableWidget::item:selected {{ background: {SURFACE_HOVER}; color: {FG}; }}
QTableWidget QLineEdit {{ min-height: 20px; }}
QHeaderView {{ background: {BG}; }}
QHeaderView::section {{ background: {BG}; color: {FG_FAINT}; border: none; border-bottom: 1px solid {BORDER};
            padding: 6px 4px; }}
QDialog, QMessageBox {{ background: {BG}; }}
QComboBox {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS_PX}px;
            padding: 6px 12px; min-height: 22px; }}
QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER};
            selection-background-color: {ACCENT}; selection-color: {BG}; outline: none; }}
"""


def pick_fonts(app: QApplication) -> None:
    """Registra a fonte base; os títulos usam Bahnschrift (Windows 10+) e caem em Segoe UI/Arial."""
    families = set(QFontDatabase.families())
    if FONT_DISPLAY not in families:
        logger.info("Fonte %s ausente; títulos usarão o fallback", FONT_DISPLAY)
    app.setFont(QFont(FONT_BODY, 10))


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    pick_fonts(app)
    app.setStyleSheet(build_stylesheet())
    return app


# ─────────────────────────────────── entrada ─────────────────────────────────
_LOG_FILE_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUPS = 3
_GUI_LOG_FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"


_APP_LOG_TAG = "_auto_clipper_app_log"


def _app_log_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _APP_LOG_TAG, False)]


def ensure_app_log() -> bool:
    """Garante que o app.log rotativo esteja anexado ao root (só empacotado). Idempotente:
    pode ser chamado de novo (a janela faz isso periodicamente) caso algum código tenha
    removido ou fechado o handler. Retorna True se o handler está ativo."""
    from logging.handlers import RotatingFileHandler

    if not runtime.is_frozen():
        return False
    root = logging.getLogger()
    for handler in _app_log_handlers():
        if getattr(handler, "stream", True) is not None:
            return True
        root.removeHandler(handler)   # fechado: refaz abaixo
    path = runtime.data_dir() / "app.log"
    try:
        file_handler = RotatingFileHandler(
            path, maxBytes=_LOG_FILE_BYTES, backupCount=_LOG_FILE_BACKUPS, encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Não foi possível abrir %s: %s", path, exc)
        return False
    file_handler.setFormatter(logging.Formatter(_GUI_LOG_FORMAT))
    file_handler.setLevel(logging.NOTSET)
    setattr(file_handler, _APP_LOG_TAG, True)
    root.addHandler(file_handler)
    return True


def setup_gui_logging() -> None:
    """Console (se existir; app "windowed" não tem stderr) + app.log rotativo quando
    empacotado, onde não há terminal para ver os logs. Recebe INFO+ de todas as lives
    (o run.log de cada sessão é um handler à parte, filtrado por thread)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO))
    if sys.stderr is not None and not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_GUI_LOG_FORMAT))
        root.addHandler(console)
    if ensure_app_log():
        logger.info("Auto-Clipper iniciado (log em %s)", runtime.data_dir() / "app.log")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Clipper (interface gráfica)")
    parser.add_argument("--demo", action="store_true", help="usa lives simuladas (não grava nada)")
    args = parser.parse_args(argv)
    setup_gui_logging()
    runtime.kill_children_on_exit()   # streamlink/ffmpeg morrem junto com o app, até em crash
    if runtime.is_frozen() and config.SESSIONS_DIR == "sessions":
        config.SESSIONS_DIR = runtime.default_sessions_dir()
    app = create_app()

    if args.demo:
        def factory(on_update):
            return FakeJobManager(on_update, seed=True, auto=True)
        settings_path = Path(tempfile.gettempdir()) / "auto-clipper-demo-settings.json"
        probe: Callable[[], str] = lambda: "NVENC (simulado)"  # noqa: E731
    else:
        from engine import JobManager
        from main import PrerequisiteError, _resolve_ffmpeg_tools

        # Mesmo ajuste da CLI: acha o FFmpeg do winget mesmo com o PATH desatualizado
        # e põe a pasta dele no PATH do processo (o Whisper chama "ffmpeg" pelo nome).
        try:
            _resolve_ffmpeg_tools()
        except PrerequisiteError as exc:
            logging.getLogger(__name__).warning("%s", exc)

        def factory(on_update):
            return JobManager(on_update=on_update)
        settings_path = SETTINGS_PATH
        probe = detect_hardware_text

    window = MainWindow(factory, settings_path=settings_path, hardware_probe=probe)
    window.resize(*MIN_WINDOW_SIZE)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
