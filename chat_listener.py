"""Etapa 2 — captura anônima e unificada do chat.

Toda plataforma é normalizada em ChatMessage. Cada plataforma tem um adaptador
que implementa a porta ChatSource (ver ADR-001 e ADR-002). O ChatListener cuida
de reconexão, backoff e de gravar chat.jsonl em tempo real.
"""
from __future__ import annotations

import json
import logging
import random
import re
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import config
from session import Session, now

__all__ = [
    "ChatMessage",
    "ChatSource",
    "TwitchIRCSource",
    "KickHistorySource",
    "YouTubeInnerTubeSource",
    "detect_platform",
    "channel_from_url",
    "make_chat_source",
    "ChatListener",
]

logger = logging.getLogger(__name__)

# --- constantes dos adaptadores (valores de protocolo, não de uso) -------------
_INITIAL_BACKOFF_S = 1.0          # backoff inicial do ChatListener (patchável em testes)
_STATS_LOG_INTERVAL_S = 60.0      # intervalo do log INFO com a contagem de mensagens
_FUTURE_TOLERANCE_S = 2.0         # wall_ts máximo à frente de now()
_SOCKET_TIMEOUT_S = 2.0           # timeout de recv
_HTTP_TIMEOUT_S = 8.0             # timeout HTTP; limita a demora para encerrar após o stop
_TWITCH_HOST = "irc.chat.twitch.tv"
_TWITCH_PORT = 6667
_TWITCH_IDLE_LIMIT_S = 400.0      # sem nenhum byte (nem PING) por tanto tempo = conexão morta
_KICK_SEEN_LIMIT = 2000           # ids de mensagens lembrados para dedupe
_KICK_CHANNEL_URL = "https://kick.com/api/v2/channels/{slug}"
_KICK_HISTORY_URL = "https://web.kick.com/api/v1/chat/{channel_id}/history"
_YT_CHAT_PAGE_URL = "https://www.youtube.com/live_chat?v={video_id}&is_popout=1"
_YT_CHAT_API_URL = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
_YT_DEFAULT_TIMEOUT_S = 2.0       # espera padrão se a resposta não trouxer timeoutMs
_YT_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


def _clamp_ts(ts: Any) -> float:
    """Instante de envio limitado a now()+2 s; now() se ausente ou inválido."""
    current = now()
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return current
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return current
    return min(value, current + _FUTURE_TOLERANCE_S)


@dataclass(frozen=True)
class ChatMessage:
    platform: str      # "twitch" | "youtube" | "kick"
    author: str
    text: str
    # Instante de ENVIO informado pela plataforma (epoch s): tmi-sent-ts (Twitch),
    # created_at (Kick), timestampUsec (YouTube). Sem ele, session.now() na recepção.
    # Nunca maior que now() + 2 s (relógio do servidor adiantado é limitado). ADR-002.
    wall_ts: float

    def to_json(self) -> str:
        """Uma linha JSON (sem quebra) para chat.jsonl."""
        return json.dumps(
            {
                "platform": self.platform,
                "author": self.author,
                "text": self.text,
                "wall_ts": self.wall_ts,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> ChatMessage:
        data = json.loads(line)
        return cls(
            platform=str(data["platform"]),
            author=str(data["author"]),
            text=str(data["text"]),
            wall_ts=float(data["wall_ts"]),
        )


class ChatSource(Protocol):
    """Porta: qualquer fonte de chat ao vivo."""

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        """Gera mensagens até a conexão cair (levanta exceção) ou stop_event ser setado
        (retorna normalmente). Esperas bloqueantes devem ter timeout <= 2 s."""
        ...


# ---------------------------------------------------------------------------
# Twitch
# ---------------------------------------------------------------------------
def _unescape_tag(value: str) -> str:
    """Desfaz o escape de valores de tags IRCv3 (\\s, \\:, \\\\, \\r, \\n)."""
    out: list[str] = []
    i = 0
    mapping = {"s": " ", ":": ";", "r": "\r", "n": "\n", "\\": "\\"}
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(mapping.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_twitch_line(line: str) -> ChatMessage | None:
    """Converte uma linha IRC em ChatMessage; None se não for PRIVMSG."""
    line = line.strip("\r\n")
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw_tags, _, line = line.partition(" ")
        for item in raw_tags[1:].split(";"):
            key, _, value = item.partition("=")
            tags[key] = value
    line = line.lstrip(" ")
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    parts = line.split(" ", 2)
    if len(parts) < 3 or parts[0] != "PRIVMSG":
        return None
    trailing = parts[2]
    if not trailing.startswith(":"):
        return None
    text = trailing[1:]
    author = _unescape_tag(tags.get("display-name", "")).strip()
    if not author:
        author = prefix.split("!", 1)[0]
    if not author:
        return None
    ts_raw = tags.get("tmi-sent-ts")
    ts: Any
    try:
        ts = int(ts_raw) / 1000.0 if ts_raw else None
    except ValueError:
        ts = None
    return ChatMessage("twitch", author, text, _clamp_ts(ts))


class TwitchIRCSource:
    """IRC anônimo: irc.chat.twitch.tv:6667, NICK justinfan<5 dígitos>,
    CAP REQ :twitch.tv/tags (para ter tmi-sent-ts e display-name), JOIN #canal.
    Responde PING com PONG. Usa só stdlib (socket, timeout <= 2 s).
    Mensagens = linhas PRIVMSG; wall_ts = tmi-sent-ts / 1000."""

    def __init__(self, channel: str) -> None:
        self.channel = channel.lower()

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        sock = socket.create_connection((_TWITCH_HOST, _TWITCH_PORT), timeout=_HTTP_TIMEOUT_S)
        try:
            sock.settimeout(_SOCKET_TIMEOUT_S)
            nick = f"justinfan{random.randint(10000, 99999)}"
            for cmd in (
                "CAP REQ :twitch.tv/tags",
                "PASS SCHMOOPIIE",
                f"NICK {nick}",
                f"JOIN #{self.channel}",
            ):
                sock.sendall((cmd + "\r\n").encode("utf-8"))
            logger.info("Twitch IRC: conectado a #%s como %s", self.channel, nick)

            buffer = b""
            last_data = now()
            while not stop_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    if now() - last_data > _TWITCH_IDLE_LIMIT_S:
                        raise ConnectionError("Twitch IRC sem dados (conexão morta)")
                    continue
                if not chunk:
                    raise ConnectionError("Twitch IRC: conexão fechada pelo servidor")
                last_data = now()
                buffer += chunk
                while b"\r\n" in buffer:
                    raw, buffer = buffer.split(b"\r\n", 1)
                    line = raw.decode("utf-8", errors="replace")
                    if line.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        continue
                    msg = _parse_twitch_line(line)
                    if msg is not None:
                        yield msg
        finally:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Kick
# ---------------------------------------------------------------------------
def _iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class _KickPager:
    """Lógica pura de dedupe/backfill do histórico da Kick.

    `fetch(cursor)` devolve o JSON da resposta ({"data": {"messages": [...], "cursor": "n"}});
    cursor None = página mais recente. `poll()` devolve só as mensagens novas
    (type == "message"), ordenadas por created_at.
    """

    def __init__(
        self,
        fetch: Callable[[str | None], dict],
        max_backfill_pages: int | None = None,
        seen_limit: int = _KICK_SEEN_LIMIT,
    ) -> None:
        self._fetch = fetch
        self._max_backfill = (
            config.KICK_MAX_BACKFILL_PAGES if max_backfill_pages is None else max_backfill_pages
        )
        self._seen_limit = seen_limit
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._primed = False

    @staticmethod
    def _unpack(payload: dict) -> tuple[list[dict], str | None]:
        data = payload.get("data", payload) or {}
        messages = data.get("messages") or []
        cursor = data.get("cursor")
        return list(messages), (str(cursor) if cursor not in (None, "") else None)

    def _remember(self, msg_id: str) -> None:
        self._seen[msg_id] = None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)

    def _overlaps(self, page: list[dict]) -> bool:
        return any(str(m.get("id")) in self._seen for m in page)

    def poll(self) -> list[dict]:
        messages, cursor = self._unpack(self._fetch(None))
        pages = [messages]
        if self._primed and messages and not self._overlaps(messages):
            # Nenhum id conhecido: o chat andou mais que uma página. Pagina para trás.
            for _ in range(self._max_backfill):
                if cursor is None:
                    break
                older, cursor = self._unpack(self._fetch(cursor))
                if not older:
                    break
                pages.append(older)
                if self._overlaps(older):
                    break
            else:
                logger.warning(
                    "Kick: backfill atingiu %d páginas sem sobreposição; pode ter perdido mensagens",
                    self._max_backfill,
                )
        if messages:
            self._primed = True

        fresh: dict[str, dict] = {}
        for page in pages:
            for m in page:
                msg_id = str(m.get("id"))
                if msg_id in self._seen or msg_id in fresh:
                    continue
                fresh[msg_id] = m
        for msg_id in fresh:
            self._remember(msg_id)
        result = [m for m in fresh.values() if m.get("type", "message") == "message"]
        result.sort(key=lambda m: _iso_to_epoch(m.get("created_at")) or 0.0)
        return result


class KickHistorySource:
    """Kick sem WebSocket (o Pusher público foi desativado; o realtime novo exige
    token de sessão de navegador). Validado em 2026-11-17:

    1) GET https://kick.com/api/v2/channels/<slug> (curl_cffi, impersonate="chrome")
       -> campo "id" = channel_id (NÃO é o chatroom.id).
    2) A cada KICK_POLL_INTERVAL_S: GET https://web.kick.com/api/v1/chat/<channel_id>/history
       -> {"data": {"messages": [...25 mais recentes...], "cursor": "<n>"}}.
       Mensagem: id, content, created_at (ISO 8601 Z), sender.username, type.
    3) Sem perda: se NENHUM id da página já foi visto (e não é a 1ª consulta), pagina
       para trás com ?cursor=<cursor> até achar sobreposição ou KICK_MAX_BACKFILL_PAGES.
    4) Emite só mensagens novas (dedupe por id; guarde os ~2000 ids mais recentes),
       ordenadas por created_at. Mensagens da 1ª consulta (backlog anterior) também
       são emitidas: o filtro por segmento descarta o que for antes da gravação."""

    def __init__(self, channel: str) -> None:
        self.channel = channel.lower()

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        from curl_cffi import requests  # import tardio

        http = requests.Session(impersonate="chrome")
        try:
            resp = http.get(
                _KICK_CHANNEL_URL.format(slug=self.channel), timeout=_HTTP_TIMEOUT_S
            )
            if resp.status_code == 404:
                raise RuntimeError(f"canal da Kick não existe: {self.channel!r}")
            if resp.status_code != 200:
                raise RuntimeError(f"Kick canal HTTP {resp.status_code}: {resp.text[:200]}")
            channel_id = resp.json().get("id")
            if channel_id is None:
                raise RuntimeError("Kick: resposta do canal sem campo 'id'")

            url = _KICK_HISTORY_URL.format(channel_id=channel_id)

            def fetch(cursor: str | None) -> dict:
                params = {"cursor": cursor} if cursor else None
                r = http.get(url, params=params, timeout=_HTTP_TIMEOUT_S)
                if r.status_code != 200:
                    raise RuntimeError(f"Kick histórico HTTP {r.status_code}: {r.text[:200]}")
                return r.json()

            pager = _KickPager(fetch)
            logger.info("Kick: acompanhando %s (channel_id=%s)", self.channel, channel_id)
            while not stop_event.is_set():
                for raw in pager.poll():
                    sender = raw.get("sender") or {}
                    yield ChatMessage(
                        "kick",
                        str(sender.get("username", "")),
                        str(raw.get("content", "")),
                        _clamp_ts(_iso_to_epoch(raw.get("created_at"))),
                    )
                stop_event.wait(config.KICK_POLL_INTERVAL_S)
        finally:
            try:
                http.close()
            except Exception:  # noqa: BLE001 - limpeza best-effort
                pass


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
_YT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"')
_YT_DATA_RE = re.compile(
    r'(?:window\["ytInitialData"\]|var\s+ytInitialData)\s*=\s*(\{.*?\})\s*;\s*</script>',
    re.DOTALL,
)
_YT_CANONICAL_RES = (
    re.compile(r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"'),
    re.compile(r'<link[^>]*href="([^"]+)"[^>]*rel="canonical"'),
)


def _first_continuation(continuations: Any) -> tuple[str | None, float | None]:
    """(continuation, timeoutMs) de continuations[0].<qualquer chave>."""
    if not continuations or not isinstance(continuations[0], dict):
        return None, None
    for value in continuations[0].values():
        if isinstance(value, dict) and value.get("continuation"):
            timeout_ms = value.get("timeoutMs")
            return str(value["continuation"]), (
                float(timeout_ms) if isinstance(timeout_ms, (int, float)) else None
            )
    return None, None


def _extract_yt_config(html: str) -> tuple[str, str]:
    """(INNERTUBE_CLIENT_VERSION, continuation inicial) da página /live_chat."""
    version = _YT_VERSION_RE.search(html)
    if not version:
        raise RuntimeError("YouTube: INNERTUBE_CLIENT_VERSION não encontrado na página")
    data_match = _YT_DATA_RE.search(html)
    if not data_match:
        raise RuntimeError("YouTube: ytInitialData não encontrado na página")
    try:
        data = json.loads(data_match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"YouTube: ytInitialData inválido: {exc}") from exc
    renderer = (data.get("contents") or {}).get("liveChatRenderer") or {}
    continuation, _ = _first_continuation(renderer.get("continuations"))
    if not continuation:
        raise RuntimeError("YouTube: continuation inicial não encontrada (chat indisponível?)")
    return version.group(1), continuation


def _parse_yt_chat_response(data: dict) -> tuple[list[ChatMessage], str | None, float]:
    """(mensagens, próxima continuation ou None, espera em segundos)."""
    live = (data.get("continuationContents") or {}).get("liveChatContinuation") or {}
    out: list[ChatMessage] = []
    for action in live.get("actions") or []:
        item = ((action or {}).get("addChatItemAction") or {}).get("item") or {}
        renderer = item.get("liveChatTextMessageRenderer")
        if not renderer:
            continue
        parts: list[str] = []
        for run in (renderer.get("message") or {}).get("runs") or []:
            if "text" in run:
                parts.append(str(run["text"]))
            elif "emoji" in run:
                emoji = run["emoji"] or {}
                shortcuts = emoji.get("shortcuts") or []
                parts.append(str(shortcuts[0]) if shortcuts else str(emoji.get("emojiId", "")))
        author = (renderer.get("authorName") or {}).get("simpleText", "")
        ts: Any
        try:
            ts = int(renderer["timestampUsec"]) / 1_000_000.0
        except (KeyError, TypeError, ValueError):
            ts = None
        out.append(ChatMessage("youtube", str(author), "".join(parts), _clamp_ts(ts)))
    continuation, timeout_ms = _first_continuation(live.get("continuations"))
    timeout_s = timeout_ms / 1000.0 if timeout_ms is not None else _YT_DEFAULT_TIMEOUT_S
    return out, continuation, timeout_s


def _resolve_youtube_video_id(session: Any, url: str) -> str:
    """video_id da URL; para canais, resolve via <canal>/live (canonical watch?v=).
    RuntimeError("canal do YouTube não está ao vivo") se não houver live."""
    _, segments, query = _parse_url(url)
    host = _parse_url(url)[0]
    if host == "youtu.be" and segments:
        return segments[0]
    if segments and segments[0] == "watch" and query.get("v"):
        return query["v"][0]
    if len(segments) >= 2 and segments[0] in ("live", "shorts") and _YT_VIDEO_ID_RE.match(segments[1]):
        return segments[1]
    if not segments:
        raise ValueError(f"URL do YouTube sem canal/vídeo reconhecível: {url!r}")
    if segments[0].startswith("@"):
        base = f"https://www.youtube.com/{segments[0]}"
    elif segments[0] == "channel" and len(segments) >= 2:
        base = f"https://www.youtube.com/channel/{segments[1]}"
    else:
        base = "https://www.youtube.com/" + "/".join(s for s in segments if s != "live")
    resp = session.get(base + "/live", timeout=_HTTP_TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError(f"YouTube {base}/live HTTP {resp.status_code}: {resp.text[:200]}")
    for pattern in _YT_CANONICAL_RES:
        match = pattern.search(resp.text)
        if match:
            found = re.search(r"watch\?v=([\w-]{11})", match.group(1))
            if found:
                return found.group(1)
            break
    raise RuntimeError("canal do YouTube não está ao vivo")


class YouTubeInnerTubeSource:
    """YouTube via InnerTube (API interna do site, sem conta; a chave/versão vem da
    própria página). chat-downloader 0.2.8 está quebrado ("Unable to parse initial
    video data"). Validado em 2026-11-17:

    1) Resolve o video_id: URL watch?v=/youtu.be/live/<id> direto; @handle ou
       /channel/<id> -> GET <url_do_canal>/live e lê <link rel="canonical" ...watch?v=ID>.
       Re-resolva a cada (re)conexão (uma live nova tem outro id).
    2) GET https://www.youtube.com/live_chat?v=<id>&is_popout=1 -> extrai
       INNERTUBE_CLIENT_VERSION e ytInitialData; continuation inicial em
       contents.liveChatRenderer.continuations[0].<qualquer>.continuation.
    3) Loop: POST https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false
       json={"context":{"client":{"clientName":"WEB","clientVersion":V}},"continuation":C}
       -> continuationContents.liveChatContinuation: actions[].addChatItemAction.item
       .liveChatTextMessageRenderer {authorName.simpleText, message.runs[] (text ou
       emoji.shortcuts[0]), timestampUsec}; próxima continuation e timeoutMs em
       continuations[0].<qualquer>. Espere min(timeoutMs, YOUTUBE_MAX_POLL_INTERVAL_S)
       com stop_event.wait. Sem continuation -> live acabou -> retorna."""

    def __init__(self, url: str) -> None:
        self.url = url

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        from curl_cffi import requests  # import tardio

        http = requests.Session(impersonate="chrome")
        try:
            video_id = _resolve_youtube_video_id(http, self.url)
            resp = http.get(
                _YT_CHAT_PAGE_URL.format(video_id=video_id), timeout=_HTTP_TIMEOUT_S
            )
            if resp.status_code != 200:
                raise RuntimeError(f"YouTube live_chat HTTP {resp.status_code}: {resp.text[:200]}")
            client_version, continuation = _extract_yt_config(resp.text)
            logger.info("YouTube: acompanhando chat do vídeo %s", video_id)
            context = {"client": {"clientName": "WEB", "clientVersion": client_version}}
            next_cont: str | None = continuation
            while next_cont and not stop_event.is_set():
                r = http.post(
                    _YT_CHAT_API_URL,
                    json={"context": context, "continuation": next_cont},
                    timeout=_HTTP_TIMEOUT_S,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"YouTube chat HTTP {r.status_code}: {r.text[:200]}")
                msgs, next_cont, timeout_s = _parse_yt_chat_response(r.json())
                yield from msgs
                if next_cont is None:
                    logger.info("YouTube: sem continuation, a live acabou")
                    return
                stop_event.wait(min(timeout_s, config.YOUTUBE_MAX_POLL_INTERVAL_S))
        finally:
            try:
                http.close()
            except Exception:  # noqa: BLE001 - limpeza best-effort
                pass


# ---------------------------------------------------------------------------
# URL / fábrica
# ---------------------------------------------------------------------------
_TWITCH_HOSTS = {"twitch.tv"}
_KICK_HOSTS = {"kick.com"}
_YOUTUBE_HOSTS = {"youtube.com", "youtu.be"}
_TWITCH_RESERVED = {"videos", "directory", "downloads", "settings", "p", "popout"}


def _parse_url(url: str) -> tuple[str, list[str], dict[str, list[str]]]:
    """(host sem www./m., segmentos do caminho, query). ValueError se vazia."""
    raw = url.strip()
    if "//" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if not host:
        raise ValueError(f"URL inválida: {url!r}")
    segments = [seg for seg in parsed.path.split("/") if seg]
    return host, segments, parse_qs(parsed.query)


def detect_platform(url: str) -> str:
    """"twitch" | "youtube" | "kick" pelo host da URL. ValueError se não suportada."""
    host, _, _ = _parse_url(url)
    if host in _TWITCH_HOSTS:
        return "twitch"
    if host in _KICK_HOSTS:
        return "kick"
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    raise ValueError(f"Plataforma não suportada: {url!r}")


def channel_from_url(url: str) -> str:
    """Nome do canal/slug a partir da URL (twitch.tv/<x>, kick.com/<x>, youtube @handle)."""
    platform = detect_platform(url)
    host, segments, query = _parse_url(url)
    if platform in ("twitch", "kick"):
        if not segments or (platform == "twitch" and segments[0].lower() in _TWITCH_RESERVED):
            raise ValueError(f"URL sem canal: {url!r}")
        return segments[0]
    # YouTube
    if host == "youtu.be":
        if segments:
            return segments[0]
    elif segments:
        first = segments[0]
        if first.startswith("@"):
            if len(first) > 1:
                return first[1:]
        elif first == "channel" and len(segments) >= 2:
            return segments[1]
        elif first == "watch" and query.get("v"):
            return query["v"][0]
        elif first in ("live", "shorts") and len(segments) >= 2:
            return segments[1]
    raise ValueError(f"URL do YouTube sem canal/vídeo reconhecível: {url!r}")


def make_chat_source(url: str) -> ChatSource:
    """Fábrica: escolhe o adaptador pela plataforma."""
    platform = detect_platform(url)
    if platform == "twitch":
        return TwitchIRCSource(channel_from_url(url))
    if platform == "kick":
        return KickHistorySource(channel_from_url(url))
    return YouTubeInnerTubeSource(url)


class ChatListener:
    """Consome uma ChatSource numa thread, com reconexão e backoff exponencial.

    - Cada mensagem vira uma linha em session.chat_path (append + flush).
    - Exceção da fonte: registra no log, espera backoff (1, 2, 4... até
      CHAT_RECONNECT_MAX_BACKOFF_S, usando stop_event.wait) e reconecta.
      O backoff volta a 1 s após receber uma mensagem com sucesso.
    - Encerra quando stop_event é setado.
    """

    def __init__(self, session: Session, source: ChatSource, stop_event: threading.Event) -> None:
        self.session = session
        self.source = source
        self.stop_event = stop_event
        self.message_count = 0
        self.reconnect_count = 0

    def run(self) -> None:
        backoff = _INITIAL_BACKOFF_S
        last_stats = now()
        with open(self.session.chat_path, "a", encoding="utf-8") as fh:
            while not self.stop_event.is_set():
                try:
                    for msg in self.source.messages(self.stop_event):
                        fh.write(msg.to_json() + "\n")
                        fh.flush()
                        self.message_count += 1
                        backoff = _INITIAL_BACKOFF_S
                        if now() - last_stats >= _STATS_LOG_INTERVAL_S:
                            last_stats = now()
                            logger.info("Chat: %d mensagens gravadas", self.message_count)
                        if self.stop_event.is_set():
                            break
                    if self.stop_event.is_set():
                        break
                    logger.warning("Chat: fonte encerrou sem erro; reconectando em %.1fs", backoff)
                except Exception as exc:  # noqa: BLE001 - a thread de chat nunca pode morrer
                    logger.warning(
                        "Chat: falha (%s: %s); reconectando em %.1fs",
                        type(exc).__name__, exc, backoff,
                    )
                    logger.debug("Traceback da falha do chat", exc_info=True)
                self.reconnect_count += 1
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, config.CHAT_RECONNECT_MAX_BACKOFF_S)
        logger.info(
            "Chat: encerrado (%d mensagens, %d reconexões)",
            self.message_count, self.reconnect_count,
        )
