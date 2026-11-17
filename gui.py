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
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

import config
import runtime
from chat_listener import channel_from_url, detect_platform
from engine import JobState, JobStatus

__all__ = [
    "FakeJobManager", "MainWindow", "JobCard", "SettingsDialog",
    "load_settings", "save_settings", "apply_settings", "main",
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
CARD_BUTTON_WIDTH = 120
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
SENSITIVITY_CHOICES = (("Baixa", 4.0), ("Média", 3.0), ("Alta", 2.2))
SETTINGS_KEYS = ("output_layout", "whisper_language", "spike_multiplier", "sessions_dir")


def default_settings() -> dict:
    """Padrões = valores ATUAIS do config.py (o settings.json só guarda o que o
    usuário mudou na tela de Ajustes; sem ele, vale o config.py)."""
    return {
        "output_layout": config.OUTPUT_LAYOUT,
        "whisper_language": config.WHISPER_LANGUAGE,
        "spike_multiplier": float(config.SPIKE_MULTIPLIER),
        "sessions_dir": config.SESSIONS_DIR,
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


# ─────────────────────────────── utilidades ──────────────────────────────────
def open_folder(path: Path) -> None:
    """Abre a pasta no Explorer (ou no gerenciador de arquivos do sistema)."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


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

    def _seed(self) -> None:
        self._create("https://www.twitch.tv/gaules", "twitch", "gaules", JobState.RECORDING,
                     "Gravando vídeo e chat", recorded_s=4350, recorded_bytes=3_600_000_000,
                     chat_messages=12480, spikes=7)
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
                self.update(job.job_id, recorded_s=job.recorded_s + 1,
                            recorded_bytes=job.recorded_bytes + 830_000,
                            chat_messages=job.chat_messages + 6,
                            spikes=job.spikes + (1 if self._ticks % 25 == 0 else 0),
                            message="Gravando vídeo e chat")
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
        self.remove_button = QPushButton("Remover")
        self.remove_button.setObjectName("ghost")
        for button in (self.stop_button, self.open_button, self.remove_button):
            button.setFixedWidth(CARD_BUTTON_WIDTH)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.job_id))
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.job_id))
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
        self._set_property(self.open_button, "primary", "true" if state == JobState.DONE and has_clips else "false")

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
    """Ajustes simples. `values()` devolve o dict no formato do settings.json."""

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ajustes")
        self.setMinimumWidth(480)
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

        form.addRow("Formato do clipe", self.layout_combo)
        form.addRow("Idioma da legenda", self.language_combo)
        form.addRow("Sensibilidade dos picos", self.sensitivity_combo)
        form.addRow("Pasta de saída", dir_row)
        hint = QLabel("Sensibilidade alta gera mais clipes, com momentos menos fortes.")
        hint.setObjectName("message")
        hint.setWordWrap(True)

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
        layout.addWidget(hint)
        layout.addWidget(buttons)

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

    def values(self) -> dict:
        return {
            "output_layout": self.layout_combo.currentData(),
            "whisper_language": self.language_combo.currentData(),
            "spike_multiplier": self.sensitivity_combo.currentData(),
            "sessions_dir": self.dir_edit.text().strip() or default_settings()["sessions_dir"],
        }


# ───────────────────────────────── janela principal ──────────────────────────
class MainWindow(QWidget):
    def __init__(self, manager_factory: Callable[[Callable[[JobStatus], None]], object],
                 settings_path: Path = SETTINGS_PATH,
                 hardware_probe: Callable[[], str] = detect_hardware_text,
                 check_ffmpeg: Callable[[], bool] = ffmpeg_available) -> None:
        super().__init__()
        self.setWindowTitle("Auto-Clipper")
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self._settings_path = Path(settings_path)
        self._settings = load_settings(self._settings_path)
        apply_settings(self._settings)
        self._cards: dict[str, JobCard] = {}

        self._bridge = UpdateBridge()
        self._bridge.updated.connect(self._on_status)
        self._build(check_ffmpeg())
        self.manager = manager_factory(self._bridge.updated.emit)
        self._start_hardware_probe(hardware_probe)
        self._disk_timer = QTimer(self)
        self._disk_timer.timeout.connect(self.refresh_footer)
        self._disk_timer.start(DISK_REFRESH_MS)
        for status in self.manager.all():       # lives que já existiam (ex.: demo)
            self._on_status(status)
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
        for label in (self.hardware_label, self.disk_label):
            foot.addWidget(label)
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

    def _on_status(self, status: JobStatus) -> None:
        card = self._cards.get(status.job_id)
        if card is None:
            card = JobCard(status)
            card.stop_requested.connect(self._stop_job)
            card.open_requested.connect(self._open_clips)
            card.remove_requested.connect(self._remove_job)
            self._cards[status.job_id] = card
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        else:
            card.update_status(status)
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

    def _remove_job(self, job_id: str) -> None:
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
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
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
        if self.active_count() and not self.confirm(
                "Sair do Auto-Clipper?", "Parar tudo e sair? O que já foi gravado fica salvo no disco."):
            event.ignore()
            return
        self._disk_timer.stop()
        self._shutdown_manager()
        event.accept()

    def _shutdown_manager(self) -> None:
        """manager.shutdown(wait=True) pode demorar: roda em thread, sem congelar a janela."""
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


def setup_gui_logging() -> None:
    """Console (se existir; app "windowed" não tem stderr) + app.log rotativo quando
    empacotado, onde não há terminal para ver os logs."""
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO))
    formatter = logging.Formatter(_GUI_LOG_FORMAT)
    if sys.stderr is not None and not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
    if runtime.is_frozen():
        try:
            file_handler = RotatingFileHandler(
                runtime.data_dir() / "app.log", maxBytes=_LOG_FILE_BYTES,
                backupCount=_LOG_FILE_BACKUPS, encoding="utf-8",
            )
        except OSError:
            return
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Clipper (interface gráfica)")
    parser.add_argument("--demo", action="store_true", help="usa lives simuladas (não grava nada)")
    args = parser.parse_args(argv)
    setup_gui_logging()
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
