"""Sessão de gravação: pastas, manifesto (session.json) e relógio.

Uma Session corresponde a uma live gravada. Ela é a única fonte de verdade sobre
os caminhos de saída e sobre os segmentos de gravação (ver ADR-004).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["RecordingSegment", "Session", "now"]


def now() -> float:
    """Relógio de parede único do projeto (epoch em segundos). Use sempre este."""
    return time.time()


@dataclass
class RecordingSegment:
    """Um arquivo contínuo de gravação (live_NN.ts).

    wall_start: instante (epoch) em que o PRIMEIRO BYTE foi escrito no arquivo.
    wall_end:   instante em que o streamlink encerrou (None enquanto grava).
    ad_breaks:  lista de (wall_ts_inicio, duracao_s) de anúncios que o
                streamlink removeu do arquivo (Twitch).
    """

    index: int
    path: Path
    wall_start: float | None = None
    wall_end: float | None = None
    ad_breaks: list[tuple[float, float]] = field(default_factory=list)

    def wall_to_offset(self, wall_ts: float) -> float | None:
        """Converte tempo de parede em segundos dentro deste arquivo.

        Retorna None se wall_ts estiver fora do segmento ou dentro de um anúncio.
        offset = wall_ts - wall_start - soma(anúncios que terminaram antes de wall_ts)
        """
        raise NotImplementedError


@dataclass
class Session:
    """Pasta de saída de uma live: sessions/<YYYYMMDD-HHMMSS>_<plataforma>_<canal>/."""

    root: Path
    url: str
    platform: str
    channel: str
    created_at: float
    segments: list[RecordingSegment] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)  # etapa -> "ok" | "erro: ..."

    # --- caminhos -------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / "session.json"

    @property
    def log_path(self) -> Path:
        return self.root / "run.log"

    @property
    def chat_path(self) -> Path:
        return self.root / "chat.jsonl"

    @property
    def spikes_path(self) -> Path:
        return self.root / "spikes.json"

    @property
    def clips_dir(self) -> Path:
        return self.root / "clips"

    @property
    def subs_dir(self) -> Path:
        return self.root / "subs"

    @property
    def final_dir(self) -> Path:
        return self.root / "final"

    # --- ciclo de vida --------------------------------------------------
    @classmethod
    def create(cls, url: str, sessions_dir: Path) -> Session:
        """Cria a pasta da sessão (e subpastas) e grava o manifesto inicial."""
        raise NotImplementedError

    @classmethod
    def load(cls, root: Path) -> Session:
        """Recarrega uma sessão a partir de session.json (usado por --process)."""
        raise NotImplementedError

    def new_segment(self) -> RecordingSegment:
        """Abre o próximo segmento (live_00.ts, live_01.ts, ...) e salva o manifesto."""
        raise NotImplementedError

    def segment_for(self, wall_ts: float) -> RecordingSegment | None:
        """Segmento cujo intervalo [wall_start, wall_end] contém wall_ts."""
        raise NotImplementedError

    def save(self) -> None:
        """Grava session.json de forma atômica (escreve .tmp e renomeia)."""
        raise NotImplementedError
