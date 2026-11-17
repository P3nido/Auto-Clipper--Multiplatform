"""Etapa 1 — monitoramento e gravação com Streamlink.

- is_live(): pergunta ao Streamlink (API Python) se a URL tem streams agora.
- Recorder: roda o executável `streamlink` em subprocess, um segmento por
  execução, e reabre um novo segmento se ele cair com a live ainda no ar.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
from session import RecordingSegment, Session, now

__all__ = ["RecorderError", "is_live", "Recorder", "remux_to_mp4"]

logger = logging.getLogger(__name__)

_FILE_POLL_INTERVAL_S = 0.2      # vigia do tamanho do arquivo (marca wall_start)
_TERMINATE_TIMEOUT_S = 5.0       # espera após terminate() antes do kill()
_RESUME_CHECK_INTERVAL_S = 10.0  # intervalo entre is_live() ao tentar reabrir
_READER_JOIN_TIMEOUT_S = 5.0
_AD_BREAK_RE = re.compile(r"Detected advertisement break of (\d+) seconds?")
# No Windows, grupo de processos novo: o Ctrl+C do terminal não atinge o streamlink.
_POPEN_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


class RecorderError(RuntimeError):
    """Falha irrecuperável de gravação (URL sem plugin, streamlink ausente...)."""


def is_live(url: str) -> bool:
    """True se a URL estiver ao vivo agora.

    Usa `streamlink.Streamlink().streams(url)`: dict vazio significa offline.
    NoPluginError vira RecorderError (URL inválida, erro fatal).
    PluginError ou erro de rede: registra no log e retorna False (tenta de novo no próximo ciclo).
    """
    # Import tardio: o módulo importa mesmo sem a lib instalada.
    try:
        import streamlink
        from streamlink.exceptions import NoPluginError, PluginError
    except ImportError as exc:
        raise RecorderError("streamlink não instalado. Rode: pip install streamlink") from exc

    try:
        streams = streamlink.Streamlink().streams(url)
    except NoPluginError as exc:
        raise RecorderError(f"Nenhum plugin do streamlink suporta a URL: {url}") from exc
    except PluginError as exc:
        logger.warning("Falha ao consultar a live (%s); tentando de novo depois.", exc)
        return False
    except Exception as exc:  # rede instável é o caso normal
        logger.warning("Erro de rede ao consultar a live: %s", exc)
        return False
    return bool(streams)


def _find_streamlink() -> str:
    """Executável do streamlink: ao lado do Python atual, senão no PATH."""
    name = "streamlink.exe" if sys.platform == "win32" else "streamlink"
    local = Path(sys.executable).parent / name
    if local.is_file():
        return str(local)
    found = shutil.which("streamlink")
    if found:
        return found
    raise RecorderError(
        "Executável do streamlink não encontrado. Instale com `pip install streamlink` "
        "no mesmo ambiente do Python ou coloque-o no PATH."
    )


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
        try:
            while not self.stop_event.is_set():
                segment = self.session.new_segment()
                code = self._record_segment(segment)
                logger.info("Segmento %d encerrado (exit code %s).", segment.index, code)
                if self.stop_event.is_set():
                    break
                if not self._wait_for_resume():
                    logger.info(
                        "Live encerrada (sem retorno em %ss).", config.RECORDING_RESUME_GRACE_S
                    )
                    break
        except Exception:
            logger.exception("Erro inesperado na gravação; encerrando.")
        finally:
            self.stop_event.set()

    def _wait_for_resume(self) -> bool:
        """Espera a live voltar por até RECORDING_RESUME_GRACE_S. False = acabou/stop."""
        deadline = time.monotonic() + config.RECORDING_RESUME_GRACE_S
        while not self.stop_event.is_set():
            if is_live(self.session.url):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(_RESUME_CHECK_INTERVAL_S, remaining))
        return False

    @staticmethod
    def _parse_ad_break(line: str) -> float | None:
        """Extrai N de 'Detected advertisement break of N seconds' (ou None)."""
        match = _AD_BREAK_RE.search(line)
        return float(match.group(1)) if match else None

    def _read_output(self, proc: subprocess.Popen, segment: RecordingSegment) -> None:
        """Lê a saída do streamlink linha a linha (roda numa thread auxiliar)."""
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            logger.info("[streamlink] %s", line)
            duration = self._parse_ad_break(line)
            if duration is not None:
                segment.ad_breaks.append((now(), duration))
                self.session.save()

    def _mark_start_if_data(self, segment: RecordingSegment) -> None:
        """Marca wall_start no primeiro instante em que o arquivo tem bytes."""
        if segment.wall_start is not None:
            return
        try:
            has_data = segment.path.stat().st_size > 0
        except OSError:
            return
        if has_data:
            segment.wall_start = now()
            self.session.save()

    def _record_segment(self, segment: RecordingSegment) -> int:
        """Executa um processo streamlink para um segmento e retorna o exit code."""
        cmd = [
            _find_streamlink(),
            self.session.url,
            config.STREAM_QUALITY,
            "-o",
            str(segment.path),
            "--force",
            "--loglevel",
            "info",
            *config.STREAMLINK_EXTRA_ARGS,
        ]
        logger.info("Iniciando gravação do segmento %d: %s", segment.index, segment.path.name)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_POPEN_FLAGS,
            )
        except FileNotFoundError as exc:
            raise RecorderError(f"Não foi possível executar o streamlink: {exc}") from exc

        # O nome deriva da thread que grava (ex.: "job-ab12cd-recorder-streamlink"): o
        # filtro de log por live (engine.py) reconhece o prefixo "job-<id>".
        reader = threading.Thread(
            target=self._read_output, args=(proc, segment),
            name=f"{threading.current_thread().name}-streamlink", daemon=True,
        )
        reader.start()
        try:
            while proc.poll() is None:
                self._mark_start_if_data(segment)
                if self.stop_event.wait(_FILE_POLL_INTERVAL_S):
                    self._stop_process(proc)
                    break
            code = proc.wait()
        finally:
            if proc.poll() is None:
                self._stop_process(proc)
            reader.join(timeout=_READER_JOIN_TIMEOUT_S)

        self._mark_start_if_data(segment)
        segment.wall_end = now()
        try:
            empty = (not segment.path.exists()) or segment.path.stat().st_size == 0
        except OSError:
            empty = True
        if empty:
            logger.warning("Segmento %d ficou vazio; removendo o arquivo.", segment.index)
            segment.path.unlink(missing_ok=True)
            segment.wall_start = None
        self.session.save()
        return code

    @staticmethod
    def _stop_process(proc: subprocess.Popen) -> None:
        """terminate(); se não sair em _TERMINATE_TIMEOUT_S, kill()."""
        proc.terminate()
        try:
            proc.wait(timeout=_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning("streamlink não encerrou a tempo; enviando kill().")
            proc.kill()
            proc.wait()


def remux_to_mp4(ts_path: Path) -> Path:
    """Opcional: remuxa .ts para .mp4 sem re-encode (-c copy -movflags +faststart)."""
    ts_path = Path(ts_path)
    mp4_path = ts_path.with_suffix(".mp4")
    cmd = [
        config.FFMPEG_BIN, "-y", "-i", str(ts_path),
        "-c", "copy", "-movflags", "+faststart", str(mp4_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError as exc:
        raise RecorderError(
            "FFmpeg não encontrado. Instale o FFmpeg e coloque-o no PATH "
            "(ou ajuste config.FFMPEG_BIN)."
        ) from exc
    if result.returncode != 0:
        raise RecorderError(f"FFmpeg falhou ao remuxar {ts_path.name}: {result.stderr.strip()}")
    return mp4_path
