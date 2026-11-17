"""Sessão de gravação: pastas, manifesto (session.json) e relógio.

Uma Session corresponde a uma live gravada. Ela é a única fonte de verdade sobre
os caminhos de saída e sobre os segmentos de gravação (ver ADR-004).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["RecordingSegment", "Session", "now"]

_CHANNEL_MAX_LEN = 40
_CHANNEL_INVALID_RE = re.compile(r"[^A-Za-z0-9_-]")
_MANIFEST_VERSION = 1


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

        Limites inclusivos [wall_start, wall_end]; wall_end None = ainda gravando
        (sem limite superior); wall_start None = None. Um anúncio (ini, dur) cobre
        [ini, ini + dur): dentro dele -> None; com ini + dur <= wall_ts, soma.
        """
        if self.wall_start is None or wall_ts < self.wall_start:
            return None
        if self.wall_end is not None and wall_ts > self.wall_end:
            return None
        removed = 0.0
        for ad_start, ad_dur in self.ad_breaks:
            ad_end = ad_start + ad_dur
            if ad_start <= wall_ts < ad_end:
                return None
            if ad_end <= wall_ts:
                removed += ad_dur
        return wall_ts - self.wall_start - removed


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
    # Protege save() (o Recorder salva de outra thread).
    _lock: threading.Lock = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

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
        # Import tardio: chat_listener importa session, então um import no topo
        # criaria um ciclo. Assim a assinatura de create() permanece a mesma.
        from chat_listener import channel_from_url, detect_platform

        platform = detect_platform(url)
        channel = channel_from_url(url)
        safe_channel = _CHANNEL_INVALID_RE.sub("_", channel)[:_CHANNEL_MAX_LEN] or "canal"
        created_at = now()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(created_at))
        root = Path(sessions_dir) / f"{stamp}_{platform}_{safe_channel}"
        root.mkdir(parents=True, exist_ok=True)
        session = cls(
            root=root, url=url, platform=platform, channel=channel, created_at=created_at
        )
        for sub in (session.clips_dir, session.subs_dir, session.final_dir):
            sub.mkdir(parents=True, exist_ok=True)
        session.save()
        return session

    @classmethod
    def load(cls, root: Path) -> Session:
        """Recarrega uma sessão a partir de session.json (usado por --process)."""
        root = Path(root)
        data = json.loads((root / "session.json").read_text(encoding="utf-8"))
        segments = [
            RecordingSegment(
                index=int(seg["index"]),
                path=root / seg["path"],
                wall_start=seg.get("wall_start"),
                wall_end=seg.get("wall_end"),
                ad_breaks=[(float(a), float(d)) for a, d in seg.get("ad_breaks", [])],
            )
            for seg in data.get("segments", [])
        ]
        return cls(
            root=root,
            url=data["url"],
            platform=data["platform"],
            channel=data["channel"],
            created_at=float(data["created_at"]),
            segments=segments,
            status=dict(data.get("status", {})),
        )

    def new_segment(self) -> RecordingSegment:
        """Abre o próximo segmento (live_00.ts, live_01.ts, ...) e salva o manifesto."""
        index = len(self.segments)
        segment = RecordingSegment(index=index, path=self.root / f"live_{index:02d}.ts")
        self.segments.append(segment)
        self.save()
        return segment

    def segment_for(self, wall_ts: float) -> RecordingSegment | None:
        """Segmento cujo intervalo [wall_start, wall_end] contém wall_ts."""
        for seg in self.segments:
            if seg.wall_start is None or wall_ts < seg.wall_start:
                continue
            if seg.wall_end is not None and wall_ts > seg.wall_end:
                continue
            return seg
        return None

    def _to_dict(self) -> dict:
        return {
            "version": _MANIFEST_VERSION,
            "url": self.url,
            "platform": self.platform,
            "channel": self.channel,
            "created_at": self.created_at,
            "status": dict(self.status),
            "segments": [
                {
                    "index": seg.index,
                    "path": _relative(seg.path, self.root),
                    "wall_start": seg.wall_start,
                    "wall_end": seg.wall_end,
                    "ad_breaks": [[a, d] for a, d in list(seg.ad_breaks)],
                }
                for seg in list(self.segments)
            ],
        }

    def save(self) -> None:
        """Grava session.json de forma atômica (escreve .tmp e renomeia)."""
        with self._lock:
            tmp = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.manifest_path)


def _relative(path: Path, root: Path) -> str:
    """Caminho relativo a root em formato POSIX (a pasta pode ser movida)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
