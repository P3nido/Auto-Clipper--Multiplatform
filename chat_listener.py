"""Etapa 2 — captura anônima e unificada do chat.

Toda plataforma é normalizada em ChatMessage. Cada plataforma tem um adaptador
que implementa a porta ChatSource (ver ADR-001 e ADR-002). O ChatListener cuida
de reconexão, backoff e de gravar chat.jsonl em tempo real.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import config
from session import Session

__all__ = [
    "ChatMessage",
    "ChatSource",
    "TwitchIRCSource",
    "KickPusherSource",
    "ChatDownloaderSource",
    "detect_platform",
    "channel_from_url",
    "make_chat_source",
    "ChatListener",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatMessage:
    platform: str      # "twitch" | "youtube" | "kick"
    author: str
    text: str
    wall_ts: float     # session.now() no momento da RECEPÇÃO

    def to_json(self) -> str:
        """Uma linha JSON (sem quebra) para chat.jsonl."""
        raise NotImplementedError

    @classmethod
    def from_json(cls, line: str) -> ChatMessage:
        raise NotImplementedError


class ChatSource(Protocol):
    """Porta: qualquer fonte de chat ao vivo."""

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        """Gera mensagens até a conexão cair (levanta exceção) ou stop_event ser setado
        (retorna normalmente). Esperas bloqueantes devem ter timeout <= 2 s."""
        ...


class TwitchIRCSource:
    """IRC anônimo: irc.chat.twitch.tv:6667, NICK justinfan<5 dígitos>, JOIN #canal.
    Responde PING com PONG. Usa só stdlib (socket). Mensagens = linhas PRIVMSG."""

    def __init__(self, channel: str) -> None:
        self.channel = channel.lower()

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        raise NotImplementedError


class KickPusherSource:
    """1) GET https://kick.com/api/v2/channels/<slug> via curl_cffi (impersonate="chrome")
       para obter chatroom.id;
    2) WebSocket wss://ws-<CLUSTER>.pusher.com/app/<KEY>?protocol=7&client=js&version=8.4.0
       e assina o canal "chatrooms.<id>.v2";
    3) eventos "App\\Events\\ChatMessageEvent" -> ChatMessage. Responde pusher:ping."""

    def __init__(self, channel: str) -> None:
        self.channel = channel.lower()

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        raise NotImplementedError


class ChatDownloaderSource:
    """YouTube via chat-downloader (ChatDownloader().get_chat(url)).
    Atenção: biblioteca sem release desde 2023. Validar com spike (ADR-002)."""

    def __init__(self, url: str) -> None:
        self.url = url

    def messages(self, stop_event: threading.Event) -> Iterator[ChatMessage]:
        raise NotImplementedError


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
    raise NotImplementedError


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
        raise NotImplementedError
