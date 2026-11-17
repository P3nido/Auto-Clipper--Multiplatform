"""Etapa 1 — monitoramento e gravação com Streamlink.

- is_live(): pergunta ao Streamlink (API Python) se a URL tem streams agora.
- Recorder: roda o executável `streamlink` em subprocess, um segmento por
  execução, e reabre um novo segmento se ele cair com a live ainda no ar.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import config
from session import RecordingSegment, Session

__all__ = ["RecorderError", "is_live", "Recorder", "remux_to_mp4"]

logger = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    """Falha irrecuperável de gravação (URL sem plugin, streamlink ausente...)."""


def is_live(url: str) -> bool:
    """True se a URL estiver ao vivo agora.

    Usa `streamlink.Streamlink().streams(url)`: dict vazio significa offline.
    NoPluginError vira RecorderError (URL inválida, erro fatal).
    PluginError ou erro de rede: registra no log e retorna False (tenta de novo no próximo ciclo).
    """
    raise NotImplementedError


class Recorder:
    """Grava a live em segmentos .ts até ela acabar ou stop_event ser setado.

    Ao terminar (live offline ou stop), SEMPRE seta stop_event para que o
    ChatListener encerre junto.
    """

    def __init__(self, session: Session, stop_event: threading.Event) -> None:
        self.session = session
        self.stop_event = stop_event

    def run(self) -> None:
        """Loop principal (roda numa thread).

        Para cada segmento:
          1. session.new_segment()
          2. inicia `streamlink <url> <STREAM_QUALITY> -o <seg.path> --loglevel info
             *STREAMLINK_EXTRA_ARGS`
          3. marca seg.wall_start quando o arquivo passa a ter tamanho > 0
          4. lê o stderr linha a linha, registra no log e extrai anúncios
             (ver _parse_ad_break)
          5. ao sair: seg.wall_end, session.save()
          6. se não houve stop e is_live() ainda for True, volta ao passo 1
        Com stop_event setado, envia terminate() ao processo (kill após 5 s).
        """
        raise NotImplementedError

    @staticmethod
    def _parse_ad_break(line: str) -> float | None:
        """Extrai N de 'Detected advertisement break of N seconds' (ou None)."""
        raise NotImplementedError

    def _record_segment(self, segment: RecordingSegment) -> int:
        """Executa um processo streamlink para um segmento e retorna o exit code."""
        raise NotImplementedError


def remux_to_mp4(ts_path: Path) -> Path:
    """Opcional: remuxa .ts para .mp4 sem re-encode (-c copy -movflags +faststart)."""
    raise NotImplementedError
