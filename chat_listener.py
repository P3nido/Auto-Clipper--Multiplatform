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


def detect_platform(url: str) -> str:
    """"twitch" | "youtube" | "kick" pelo host da URL. ValueError se não suportada."""
    raise NotImplementedError


def channel_from_url(url: str) -> str:
    """Nome do canal/slug a partir da URL (twitch.tv/<x>, kick.com/<x>, youtube @handle)."""
    raise NotImplementedError


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
