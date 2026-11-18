"""Postagem dos clipes no YouTube (Shorts) pela YouTube Data API v3 (ADR-012).

Opcional: sem conectar, o app funciona como antes. A credencial OAuth (client_secret)
é do PRÓPRIO usuário, criada no Google Cloud, e fica em runtime.data_dir(), nunca no
.exe nem no git. O login usa google-auth-oauthlib (app instalado + loopback). O
upload retomável é feito à mão sobre AuthorizedSession (sem google-api-python-client).

Não depende de Qt nem do engine: a GUI chama `UploadQueue.submit` e recebe `UploadEvent`s.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import config
import runtime

__all__ = [
    "SCOPES", "UploadError", "AuthError", "QuotaError", "VideoMeta", "UploadRecord",
    "UploadEvent", "client_secret_path", "token_path", "clip_index", "build_meta",
    "records_path", "load_records", "save_record", "is_posted", "YouTubeClient", "UploadQueue",
]

logger = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/youtube.upload",)
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
WATCH_URL = "https://youtube.com/shorts/{video_id}"
PRIVACY_CHOICES = ("public", "unlisted", "private")
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
REVOKE_TIMEOUT_S = 10
TITLE_MAX = 100
DESCRIPTION_MAX = 5000            # a API limita em bytes (UTF-8); build_meta respeita os dois
RECORDS_FILE = "youtube.json"
PLATFORM_LABELS = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}
RETRY_STATUSES = frozenset({500, 502, 503, 504})
QUOTA_REASONS = frozenset({"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"})
QUOTA_MESSAGE = "Cota diária do YouTube esgotada; tente amanhã"
AUTH_MISSING_MESSAGE = "Componente de login do Google ausente"
AUTH_EXPIRED_MESSAGE = "Login do YouTube expirou; conecte de novo em Ajustes"
AUTH_NOT_CONNECTED_MESSAGE = "YouTube não conectado; conecte em Ajustes"
LOGIN_PROMPT_MESSAGE = ("Abrimos o navegador: entre na sua conta do Google e autorize o "
                        "Auto-Clipper a postar vídeos.")
LOGIN_SUCCESS_MESSAGE = "Login concluído. Pode fechar esta aba e voltar ao Auto-Clipper."
QUEUE_POLL_S = 0.5
_CLIP_INDEX_RE = re.compile(r"^clip_(\d+)", re.IGNORECASE)
_RECORDS_LOCK = threading.Lock()


class UploadError(Exception):
    """Falha no envio (rede esgotou as tentativas, resposta inesperada, arquivo sumiu...)."""


class AuthError(UploadError):
    """Sem credencial/login, ou login expirado/revogado (invalid_grant). Reconectar."""


class QuotaError(UploadError):
    """Cota diária esgotada (403 quotaExceeded / uploadLimitExceeded / dailyLimitExceeded)."""


@dataclass(frozen=True)
class VideoMeta:
    title: str                     # já truncado a 100 caracteres, sem "<" ">"
    description: str               # até 5000 caracteres, sem "<" ">"
    tags: tuple[str, ...]
    privacy: str                   # "public" | "unlisted" | "private"
    category_id: str               # config.YOUTUBE_CATEGORY_ID


@dataclass(frozen=True)
class UploadRecord:
    clip_name: str                 # nome do arquivo em <sessão>/final/
    video_id: str
    url: str                       # WATCH_URL formatada
    privacy: str                   # o privacyStatus que o YouTube DEVOLVEU (pode diferir do pedido)
    uploaded_at: float             # time.time()


@dataclass(frozen=True)
class UploadEvent:
    kind: str                      # "queued" | "started" | "progress" | "done" | "failed"
    clip: Path
    done_bytes: int = 0
    total_bytes: int = 0
    record: UploadRecord | None = None
    error: str | None = None       # mensagem amigável em pt-BR (kind == "failed")
    error_kind: str | None = None  # "auth" | "quota" | "other"


# ─────────────────────────────── caminhos ────────────────────────────────
def client_secret_path() -> Path:
    """runtime.data_dir() / config.YOUTUBE_CLIENT_SECRET_FILE."""
    return runtime.data_dir() / config.YOUTUBE_CLIENT_SECRET_FILE


def token_path() -> Path:
    """runtime.data_dir() / config.YOUTUBE_TOKEN_FILE."""
    return runtime.data_dir() / config.YOUTUBE_TOKEN_FILE


# ─────────────────────────────── metadados ───────────────────────────────
def clip_index(clip: Path) -> int | None:
    """Número do clipe a partir do nome ("clip_03_seg0_12m30s.mp4" → 3); None se não casar."""
    match = _CLIP_INDEX_RE.match(Path(clip).stem)
    return int(match.group(1)) if match else None


def _strip_angle(text: str) -> str:
    return text.replace("<", "").replace(">", "")


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Corta em `max_bytes` UTF-8 sem partir um caractere ao meio."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _render(template: str, **fields: object) -> str:
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        logger.warning("Modelo de título/descrição inválido em config.py; usando o texto cru")
        return template


def build_meta(clip: Path, *, channel: str, platform: str, privacy: str | None = None,
               title: str | None = None, when: float | None = None) -> VideoMeta:
    """Monta os metadados a partir de config.YOUTUBE_TITLE_TEMPLATE / _DESCRIPTION_TEMPLATE.

    Campos dos modelos: {channel}, {platform} (rótulo "Twitch"/"Kick"/"YouTube"), {n}
    (clip_index ou 1), {date} (dd/mm/aaaa de `when`, padrão = mtime do clipe).
    `title` explícito (editado pelo usuário) substitui o modelo. Remove "<" e ">" (o YouTube
    rejeita), colapsa espaços, trunca título em 100 e descrição em 5000. Título vazio → nome
    do arquivo sem extensão. `privacy` None → config.YOUTUBE_PRIVACY; valor fora de
    PRIVACY_CHOICES → ValueError. Tags = config.YOUTUBE_TAGS + nome do canal (sem duplicar).
    """
    clip = Path(clip)
    chosen_privacy = privacy if privacy is not None else config.YOUTUBE_PRIVACY
    if chosen_privacy not in PRIVACY_CHOICES:
        raise ValueError(f"Visibilidade inválida: {chosen_privacy!r} (use {', '.join(PRIVACY_CHOICES)})")
    if when is None:
        try:
            when = clip.stat().st_mtime
        except OSError:
            when = time.time()
    fields = {
        "channel": channel,
        "platform": PLATFORM_LABELS.get(platform, platform.capitalize()),
        "n": clip_index(clip) or 1,
        "date": time.strftime("%d/%m/%Y", time.localtime(when)),
    }

    raw_title = title if title is not None else _render(config.YOUTUBE_TITLE_TEMPLATE, **fields)
    clean_title = " ".join(_strip_angle(raw_title).split())[:TITLE_MAX].rstrip()
    if not clean_title:
        clean_title = " ".join(_strip_angle(clip.stem).split())[:TITLE_MAX].rstrip() or clip.stem

    raw_description = _render(config.YOUTUBE_DESCRIPTION_TEMPLATE, **fields)
    lines = [" ".join(line.split()) for line in _strip_angle(raw_description).splitlines()]
    description = "\n".join(lines).strip()[:DESCRIPTION_MAX]
    description = _truncate_bytes(description, DESCRIPTION_MAX).rstrip()

    tags: list[str] = []
    seen: set[str] = set()
    for tag in (*config.YOUTUBE_TAGS, channel):
        clean = " ".join(_strip_angle(str(tag)).split())
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            tags.append(clean)
    return VideoMeta(title=clean_title, description=description, tags=tuple(tags),
                     privacy=chosen_privacy, category_id=str(config.YOUTUBE_CATEGORY_ID))


# ──────────────────────── registro do que foi postado ─────────────────────
def records_path(session_dir: Path) -> Path:
    """session_dir / "youtube.json"."""
    return Path(session_dir) / RECORDS_FILE


def _record_from_dict(name: str, data: object) -> UploadRecord | None:
    if not isinstance(data, dict):
        return None
    try:
        return UploadRecord(clip_name=str(data.get("clip_name") or name),
                            video_id=str(data["video_id"]), url=str(data["url"]),
                            privacy=str(data.get("privacy", "")),
                            uploaded_at=float(data.get("uploaded_at", 0.0)))
    except (KeyError, TypeError, ValueError):
        return None


def load_records(session_dir: Path) -> dict[str, UploadRecord]:
    """clip_name → UploadRecord. Arquivo ausente/corrompido → {} (corrompido: logger.warning)."""
    path = records_path(session_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("youtube.json inválido em %s; ignorando", path)
        return {}
    if not isinstance(raw, dict):
        logger.warning("youtube.json com formato inesperado em %s; ignorando", path)
        return {}
    records: dict[str, UploadRecord] = {}
    for name, data in raw.items():
        record = _record_from_dict(str(name), data)
        if record is not None:
            records[str(name)] = record
    return records


def save_record(session_dir: Path, record: UploadRecord) -> None:
    """Acrescenta/substitui o registro e grava ATOMICAMENTE (tmp + os.replace), sob um lock
    de módulo (threads da GUI e da fila podem gravar ao mesmo tempo)."""
    path = records_path(session_dir)
    with _RECORDS_LOCK:
        records = load_records(session_dir)
        records[record.clip_name] = record
        payload = {name: asdict(rec) for name, rec in records.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def is_posted(clip: Path, session_dir: Path | None = None) -> UploadRecord | None:
    """Registro do clipe, se já postado. session_dir None → clip.parent.parent."""
    clip = Path(clip)
    base = Path(session_dir) if session_dir is not None else clip.parent.parent
    return load_records(base).get(clip.name)


# ─────────────────────────────── cliente ─────────────────────────────────
def _error_info(response: object) -> tuple[str, str]:
    """(reason, mensagem) do corpo de erro do Google: error.errors[0].reason / error.message."""
    try:
        body = response.json()  # type: ignore[attr-defined]
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            return "", str(error)
        errors = error.get("errors") or [{}]
        reason = str(errors[0].get("reason", "")) if isinstance(errors[0], dict) else ""
        return reason, str(error.get("message", ""))
    except Exception:  # noqa: BLE001 - corpo ausente/não-JSON
        return "", str(getattr(response, "text", "") or "")[:200]


def _google_errors() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """(erros de renovação de login, erros de transporte do google-auth); vazios sem o pacote."""
    try:
        from google.auth import exceptions as gexc
    except ImportError:
        return (), ()
    return (gexc.RefreshError,), (gexc.TransportError,)


class YouTubeClient:
    """Login OAuth e upload retomável. Uma instância pode ser usada por uma thread por vez."""

    def __init__(self, client_secret: Path | None = None, token: Path | None = None, *,
                 session_factory: Callable[[object], object] | None = None,
                 credentials_loader: Callable[[], object] | None = None) -> None:
        """Padrões: client_secret_path() / token_path(). `session_factory(credentials)` cria a
        sessão HTTP (padrão: google.auth.transport.requests.AuthorizedSession) — injeção para testes.
        `credentials_loader()` (só testes) devolve as credenciais no lugar de ler o token."""
        self._client_secret = Path(client_secret) if client_secret is not None else client_secret_path()
        self._token = Path(token) if token is not None else token_path()
        self._session_factory = session_factory
        self._credentials_loader = credentials_loader
        self._sleep: Callable[[float], None] = time.sleep

    # --- estado ---
    def is_configured(self) -> bool:
        """O arquivo client_secret existe."""
        return self._client_secret.is_file()

    def is_connected(self) -> bool:
        """Há token salvo com refresh_token (não faz rede)."""
        try:
            data = json.loads(self._token.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and bool(data.get("refresh_token"))

    # --- credencial do app ---
    def install_client_secret(self, source: Path) -> None:
        """Valida o JSON escolhido pelo usuário (precisa ter a chave "installed" com client_id
        e client_secret; um cliente do tipo "web" → AuthError explicando que tem de ser
        "App para computador") e copia para client_secret_path()."""
        try:
            raw = Path(source).read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
        except OSError as exc:
            raise AuthError(f"Não consegui ler o arquivo escolhido: {exc}") from exc
        except ValueError as exc:
            raise AuthError("O arquivo escolhido não é um JSON válido. Baixe de novo a credencial "
                            "no Google Cloud.") from exc
        installed = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(installed, dict):
            if isinstance(data, dict) and "web" in data:
                raise AuthError('Essa credencial é do tipo "Aplicativo da Web". Crie outra do tipo '
                                '"App para computador" no Google Cloud.')
            raise AuthError("Esse arquivo não parece ser uma credencial OAuth do Google "
                            '(tipo "App para computador").')
        if not installed.get("client_id") or not installed.get("client_secret"):
            raise AuthError("A credencial está incompleta (faltam client_id ou client_secret).")
        self._client_secret.parent.mkdir(parents=True, exist_ok=True)
        try:
            changed = self._client_secret.is_file() and self._client_secret.read_bytes() != raw
        except OSError:
            changed = False
        tmp = self._client_secret.with_name(self._client_secret.name + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, self._client_secret)
        if changed:       # token de outro cliente OAuth não vale para este
            self._delete_token()
        logger.info("Credencial do YouTube instalada em %s", self._client_secret)

    # --- login ---
    def connect(self, *, open_browser: bool = True) -> None:
        """Abre o navegador no consentimento do Google (InstalledAppFlow.from_client_secrets_file
        + run_local_server(port=0, open_browser=..., timeout_seconds=config.YOUTUBE_LOGIN_TIMEOUT_S,
        mensagens em pt-BR)) e salva o token. Bloqueia: a GUI chama numa thread.
        Sem client_secret → AuthError. Usuário recusou/tempo esgotou → AuthError."""
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise AuthError(AUTH_MISSING_MESSAGE) from exc
        if not self.is_configured():
            raise AuthError("Falta o arquivo de credencial do Google. Escolha-o em Ajustes.")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(self._client_secret), list(SCOPES))
            creds = flow.run_local_server(
                port=0, open_browser=open_browser,
                timeout_seconds=config.YOUTUBE_LOGIN_TIMEOUT_S,
                authorization_prompt_message=LOGIN_PROMPT_MESSAGE,
                success_message=LOGIN_SUCCESS_MESSAGE,
                prompt="consent",       # garante o refresh_token mesmo em reconexões
            )
        except Exception as exc:  # noqa: BLE001 - recusa, timeout, rede, JSON ruim
            logger.warning("Login do YouTube não concluído: %s", type(exc).__name__)
            raise AuthError("Não deu para concluir o login do Google (recusado, fechado ou "
                            "demorou demais). Tente de novo.") from exc
        if not getattr(creds, "refresh_token", None):
            raise AuthError("O Google não devolveu a permissão de uso contínuo. Tente conectar de novo.")
        self._save_token(creds)
        logger.info("YouTube conectado")

    def disconnect(self) -> None:
        """Apaga o token local (tenta revogar em https://oauth2.googleapis.com/revoke, sem falhar)."""
        try:
            data = json.loads(self._token.read_text(encoding="utf-8"))
            secret = data.get("refresh_token") or data.get("token")
        except (OSError, ValueError, AttributeError):
            secret = None
        if secret:
            try:
                import requests
                requests.post(REVOKE_URL, params={"token": secret}, timeout=REVOKE_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - revogar é cortesia; sem rede, segue apagando
                logger.info("Não consegui revogar o token no Google (segue apagando o local)")
        self._delete_token()
        logger.info("YouTube desconectado")

    def _delete_token(self) -> None:
        try:
            self._token.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Não consegui apagar o token em %s", self._token)

    def _save_token(self, creds: object) -> None:
        self._token.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token.with_name(self._token.name + ".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[attr-defined]
        os.replace(tmp, self._token)

    # --- credenciais para o upload ---
    def _credentials(self) -> object:
        """Credenciais válidas (renova se expiradas). AuthError se não der."""
        injected = self._credentials_loader is not None
        if self._credentials_loader is not None:
            creds = self._credentials_loader()
        else:
            try:
                from google.oauth2.credentials import Credentials
            except ImportError as exc:
                raise AuthError(AUTH_MISSING_MESSAGE) from exc
            if not self._token.is_file():
                raise AuthError(AUTH_NOT_CONNECTED_MESSAGE)
            try:
                creds = Credentials.from_authorized_user_file(str(self._token), list(SCOPES))
            except (ValueError, OSError) as exc:
                self._delete_token()
                raise AuthError(AUTH_EXPIRED_MESSAGE) from exc
        if getattr(creds, "valid", False):
            return creds
        if not getattr(creds, "refresh_token", None):
            self._delete_token()
            raise AuthError(AUTH_EXPIRED_MESSAGE)
        refresh_errors, transport_errors = _google_errors()
        try:
            from google.auth.transport.requests import Request
        except ImportError as exc:
            raise AuthError(AUTH_MISSING_MESSAGE) from exc
        try:
            creds.refresh(Request())  # type: ignore[attr-defined]
        except refresh_errors as exc:
            logger.warning("Renovação do login do YouTube recusada: %s", type(exc).__name__)
            self._delete_token()
            raise AuthError(AUTH_EXPIRED_MESSAGE) from exc
        except (*transport_errors, OSError) as exc:
            raise UploadError(f"Sem conexão para renovar o login do YouTube: {exc}") from exc
        if not injected:
            try:
                self._save_token(creds)
            except OSError:
                logger.warning("Não consegui gravar o token renovado em %s", self._token)
        return creds

    def _make_session(self, creds: object) -> object:
        if self._session_factory is not None:
            return self._session_factory(creds)
        try:
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise AuthError(AUTH_MISSING_MESSAGE) from exc
        return AuthorizedSession(creds)

    # --- upload ---
    def upload(self, clip: Path, meta: VideoMeta, *,
               on_progress: Callable[[int, int], None] | None = None,
               stop_event: threading.Event | None = None) -> UploadRecord:
        """Envia o clipe e devolve o registro (NÃO grava youtube.json: quem chama decide).

        Protocolo (https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol):
          1. POST UPLOAD_URL?uploadType=resumable&part=snippet,status com JSON
             {snippet:{title,description,tags,categoryId}, status:{privacyStatus,
             selfDeclaredMadeForKids: false}} e cabeçalhos X-Upload-Content-Length /
             X-Upload-Content-Type: video/mp4 → Location = URL da sessão.
          2. PUT em blocos de config.YOUTUBE_CHUNK_BYTES com Content-Range; 308 = continuar a
             partir do "Range: bytes=0-N" devolvido; 200/201 = fim (JSON com id e status).
          3. Erro de rede ou 500/502/503/504 → consulta o estado (PUT vazio com
             "Content-Range: bytes */TOTAL") e retoma, até config.YOUTUBE_MAX_RETRIES tentativas
             com backoff exponencial (config.YOUTUBE_RETRY_BASE_S, injetável via self._sleep
             para testes).
          4. 401 / RefreshError(invalid_grant) → apaga o token e levanta AuthError.
             403 com reason quotaExceeded/uploadLimitExceeded/dailyLimitExceeded → QuotaError.
             Outros 4xx → UploadError com a mensagem do Google.
        stop_event setado entre blocos → UploadError("cancelado").
        on_progress(bytes_enviados, total) após cada bloco.
        """
        clip = Path(clip)
        try:
            total = clip.stat().st_size
        except OSError as exc:
            raise UploadError(f"Não encontrei o clipe {clip.name}: {exc}") from exc
        if total <= 0:
            raise UploadError(f"O clipe {clip.name} está vazio.")
        creds = self._credentials()
        session = self._make_session(creds)
        refresh_errors, transport_errors = _google_errors()
        net_errors: tuple[type[BaseException], ...] = (OSError, *transport_errors)
        try:
            session_url = self._start_session(session, meta, total, net_errors, stop_event)
            response = self._send_chunks(session, session_url, clip, total, net_errors,
                                         on_progress, stop_event)
        except refresh_errors as exc:
            logger.warning("Login do YouTube recusado durante o envio: %s", type(exc).__name__)
            self._delete_token()
            raise AuthError(AUTH_EXPIRED_MESSAGE) from exc
        return self._record_from(response, clip, meta)

    @staticmethod
    def _check_stop(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise UploadError("cancelado")

    def _backoff(self, attempt: int, what: str) -> None:
        delay = config.YOUTUBE_RETRY_BASE_S * (2 ** (attempt - 1))
        logger.warning("%s; nova tentativa %d/%d em %.0fs", what, attempt,
                       config.YOUTUBE_MAX_RETRIES, delay)
        self._sleep(delay)

    def _raise_for_error(self, response: object) -> None:
        """Converte um erro HTTP (não-retentável) na exceção tipada certa."""
        status = int(response.status_code)  # type: ignore[attr-defined]
        reason, message = _error_info(response)
        if status == 401:
            self._delete_token()
            raise AuthError(AUTH_EXPIRED_MESSAGE)
        if status == 403 and reason in QUOTA_REASONS:
            raise QuotaError(QUOTA_MESSAGE)
        if status == 404:
            raise UploadError("A sessão de envio expirou no YouTube; tente enviar de novo.")
        detail = message or reason or "sem detalhes"
        raise UploadError(f"O YouTube recusou o envio (erro {status}): {detail}")

    def _start_session(self, session: object, meta: VideoMeta, total: int,
                       net_errors: tuple[type[BaseException], ...],
                       stop_event: threading.Event | None) -> str:
        body = {
            "snippet": {"title": meta.title, "description": meta.description,
                        "tags": list(meta.tags), "categoryId": meta.category_id},
            "status": {"privacyStatus": meta.privacy, "selfDeclaredMadeForKids": False},
        }
        headers = {"X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"}
        attempt = 0
        while True:
            self._check_stop(stop_event)
            try:
                response = session.post(  # type: ignore[attr-defined]
                    UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                    json=body, headers=headers, timeout=config.YOUTUBE_HTTP_TIMEOUT_S)
            except net_errors as exc:
                response, what = None, f"Falha de rede ao iniciar o envio ({type(exc).__name__})"
            else:
                what = f"YouTube respondeu {response.status_code} ao iniciar o envio"
            if response is not None and response.status_code not in RETRY_STATUSES:
                if response.status_code in (200, 201):
                    location = response.headers.get("Location")
                    if not location:
                        raise UploadError("O YouTube não devolveu o endereço de envio.")
                    return str(location)
                self._raise_for_error(response)
            attempt += 1
            if attempt > config.YOUTUBE_MAX_RETRIES:
                raise UploadError("Não consegui iniciar o envio ao YouTube (rede instável). "
                                  "Tente de novo mais tarde.")
            self._backoff(attempt, what)

    @staticmethod
    def _received_offset(response: object) -> int:
        """Próximo byte a enviar a partir do cabeçalho Range ("bytes=0-N"); ausente = 0."""
        headers = response.headers  # type: ignore[attr-defined]
        value = headers.get("Range") or headers.get("range")
        if not value:
            return 0
        match = re.search(r"-(\d+)\s*$", str(value))
        return int(match.group(1)) + 1 if match else 0

    def _send_chunks(self, session: object, session_url: str, clip: Path, total: int,
                     net_errors: tuple[type[BaseException], ...],
                     on_progress: Callable[[int, int], None] | None,
                     stop_event: threading.Event | None) -> object:
        offset = 0
        attempt = 0
        need_query = False
        with clip.open("rb") as handle:
            while True:
                self._check_stop(stop_event)
                try:
                    if need_query:
                        response = session.put(  # type: ignore[attr-defined]
                            session_url, data=b"",
                            headers={"Content-Range": f"bytes */{total}"},
                            timeout=config.YOUTUBE_HTTP_TIMEOUT_S)
                    else:
                        end = min(offset + config.YOUTUBE_CHUNK_BYTES, total)
                        handle.seek(offset)
                        data = handle.read(end - offset)
                        response = session.put(  # type: ignore[attr-defined]
                            session_url, data=data,
                            headers={"Content-Range": f"bytes {offset}-{end - 1}/{total}",
                                     "Content-Type": "video/mp4"},
                            timeout=config.YOUTUBE_HTTP_TIMEOUT_S)
                except net_errors as exc:
                    response, what = None, f"Falha de rede no envio ({type(exc).__name__})"
                else:
                    what = f"YouTube respondeu {response.status_code} no envio"
                status = response.status_code if response is not None else 0
                if response is None or status in RETRY_STATUSES:
                    attempt += 1
                    if attempt > config.YOUTUBE_MAX_RETRIES:
                        raise UploadError("O envio ao YouTube falhou várias vezes seguidas "
                                          "(rede instável). Tente de novo mais tarde.")
                    self._backoff(attempt, what)
                    need_query = True
                    continue
                if status in (200, 201):
                    if on_progress is not None:
                        on_progress(total, total)
                    return response
                if status == 308:
                    new_offset = min(self._received_offset(response), total)
                    if new_offset > offset:
                        attempt = 0
                    elif not need_query:
                        # Mandou um bloco e o YouTube não registrou nada: conta como falha,
                        # senão o mesmo bloco seria reenviado para sempre.
                        attempt += 1
                        if attempt > config.YOUTUBE_MAX_RETRIES:
                            raise UploadError("O YouTube não está aceitando o envio deste clipe. "
                                              "Tente de novo mais tarde.")
                        self._backoff(attempt, "YouTube não registrou o bloco enviado")
                    offset = new_offset
                    need_query = False
                    if on_progress is not None:
                        on_progress(offset, total)
                    continue
                self._raise_for_error(response)

    @staticmethod
    def _record_from(response: object, clip: Path, meta: VideoMeta) -> UploadRecord:
        try:
            data = response.json()  # type: ignore[attr-defined]
            video_id = str(data["id"])
        except Exception as exc:  # noqa: BLE001
            raise UploadError("O YouTube terminou o envio mas não devolveu o vídeo.") from exc
        status = data.get("status")
        privacy = status.get("privacyStatus") if isinstance(status, dict) else None
        return UploadRecord(clip_name=clip.name, video_id=video_id,
                            url=WATCH_URL.format(video_id=video_id),
                            privacy=str(privacy or meta.privacy), uploaded_at=time.time())


# ─────────────────────────────── fila ────────────────────────────────────
@dataclass(frozen=True)
class _Job:
    clip: Path
    meta: VideoMeta
    session_dir: Path


class UploadQueue:
    """Uma thread ("yt-upload"), um envio por vez. Eventos saem por `on_event` (de outra thread:
    a GUI repassa por Signal). Grava youtube.json a cada envio concluído."""

    def __init__(self, client: YouTubeClient,
                 on_event: Callable[[UploadEvent], None] | None = None) -> None:
        self._client = client
        self._on_event = on_event
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._active: set[Path] = set()          # na fila + em envio
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._cancel = threading.Event()         # cancela o envio em andamento
        self._thread: threading.Thread | None = None

    def submit(self, clip: Path, meta: VideoMeta, session_dir: Path | None = None) -> bool:
        """Enfileira. False (sem enfileirar) se já postado (is_posted) ou já na fila/em envio.
        Emite "queued". Inicia a thread na primeira chamada."""
        clip = Path(clip)
        base = Path(session_dir) if session_dir is not None else clip.parent.parent
        if self._closed.is_set():
            return False
        if is_posted(clip, base) is not None:
            return False
        with self._lock:
            if clip in self._active:
                return False
            self._active.add(clip)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="yt-upload", daemon=True)
                self._thread.start()
            self._queue.put(_Job(clip, meta, base))
        self._emit(UploadEvent("queued", clip))
        return True

    def pending(self) -> int:
        """Na fila + em envio."""
        with self._lock:
            return len(self._active)

    def is_queued(self, clip: Path) -> bool:
        with self._lock:
            return Path(clip) in self._active

    def shutdown(self, wait: bool = False, timeout_s: float = 10.0) -> None:
        """Cancela o envio atual (stop_event) e descarta a fila."""
        self._closed.set()
        self._cancel.set()
        self._drain(None)
        thread = self._thread
        if wait and thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout_s)

    # Regras de erro na thread:
    #   AuthError  → "failed"(error_kind="auth") para o atual E para todos os pendentes (fila esvazia);
    #   QuotaError → idem com error_kind="quota" ("Cota diária do YouTube esgotada; tente amanhã");
    #   outro      → "failed"(error_kind="other") só para o atual e segue a fila.
    def _emit(self, event: UploadEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - callback da GUI não pode matar a thread
            logger.exception("Falha ao entregar o evento de envio %s", event.kind)

    def _drain(self, failure: tuple[str, str] | None) -> None:
        """Esvazia a fila; com `failure` (mensagem, tipo) emite "failed" para cada pendente."""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                self._active.discard(job.clip)
            if failure is not None:
                self._emit(UploadEvent("failed", job.clip, error=failure[0], error_kind=failure[1]))

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                job = self._queue.get(timeout=QUEUE_POLL_S)
            except queue.Empty:
                continue
            try:
                self._process(job)
            except Exception:  # noqa: BLE001 - a thread nunca morre calada
                logger.exception("Erro inesperado na fila de envio ao YouTube")
            finally:
                with self._lock:
                    self._active.discard(job.clip)

    def _process(self, job: _Job) -> None:
        clip = job.clip
        try:
            total = clip.stat().st_size
        except OSError:
            total = 0
        self._emit(UploadEvent("started", clip, total_bytes=total))

        def progress(done: int, size: int) -> None:
            self._emit(UploadEvent("progress", clip, done_bytes=done, total_bytes=size))

        try:
            record = self._client.upload(clip, job.meta, on_progress=progress,
                                         stop_event=self._cancel)
        except AuthError as exc:
            message = str(exc) or AUTH_EXPIRED_MESSAGE
            self._emit(UploadEvent("failed", clip, error=message, error_kind="auth"))
            self._drain((message, "auth"))
            return
        except QuotaError:
            self._emit(UploadEvent("failed", clip, error=QUOTA_MESSAGE, error_kind="quota"))
            self._drain((QUOTA_MESSAGE, "quota"))
            return
        except UploadError as exc:
            logger.warning("Envio de %s falhou: %s", clip.name, exc)
            self._emit(UploadEvent("failed", clip, error=str(exc), error_kind="other"))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro inesperado ao enviar %s", clip.name)
            self._emit(UploadEvent("failed", clip, error=f"Erro inesperado: {exc}", error_kind="other"))
            return
        try:
            save_record(job.session_dir, record)
        except OSError:
            logger.exception("Enviado, mas não consegui gravar o youtube.json em %s", job.session_dir)
        logger.info("Clipe %s postado: %s", clip.name, record.url)
        self._emit(UploadEvent("done", clip, done_bytes=total, total_bytes=total, record=record))
