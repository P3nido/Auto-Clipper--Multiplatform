from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import chat_listener
from chat_listener import (
    ChatListener,
    ChatMessage,
    KickHistorySource,
    TwitchIRCSource,
    YouTubeInnerTubeSource,
    _KickPager,
    _clamp_ts,
    _extract_yt_config,
    _parse_twitch_line,
    _parse_yt_chat_response,
    _resolve_youtube_video_id,
    make_chat_source,
)
from session import Session, now


# --- ChatMessage ------------------------------------------------------------
def test_json_roundtrip_emoji_e_aspas():
    msg = ChatMessage("twitch", 'Ana "A"', 'olá 😂 "aspas" \\ e\nquebra', 1700000000.5)
    line = msg.to_json()
    assert "\n" not in line
    assert "😂" in line  # ensure_ascii=False
    assert set(json.loads(line)) == {"platform", "author", "text", "wall_ts"}
    assert ChatMessage.from_json(line) == msg


# --- _clamp_ts --------------------------------------------------------------
def test_clamp_ts():
    t = now()
    assert _clamp_ts(t - 100) == t - 100
    assert _clamp_ts(t + 1000) <= now() + 2.0
    assert abs(_clamp_ts(None) - now()) < 1
    assert abs(_clamp_ts("abc") - now()) < 1
    assert abs(_clamp_ts(float("nan")) - now()) < 1


# --- Twitch -----------------------------------------------------------------
def test_twitch_com_tags():
    ts_ms = int((now() - 10) * 1000)
    line = (
        f"@badge-info=;display-name=Fulano;tmi-sent-ts={ts_ms};user-id=1 "
        ":fulano!fulano@fulano.tmi.twitch.tv PRIVMSG #canal :texto aqui"
    )
    msg = _parse_twitch_line(line)
    assert msg == ChatMessage("twitch", "Fulano", "texto aqui", ts_ms / 1000.0)


def test_twitch_sem_tags_e_dois_pontos():
    before = now()
    msg = _parse_twitch_line(":fulano!fulano@x.tmi.twitch.tv PRIVMSG #canal :oi: tudo :) bem")
    assert msg is not None
    assert msg.author == "fulano"
    assert msg.text == "oi: tudo :) bem"
    assert before <= msg.wall_ts <= now() + 2


def test_twitch_display_name_vazio_usa_nick():
    msg = _parse_twitch_line("@display-name=;tmi-sent-ts=1 :nick!n@n PRIVMSG #c :x")
    assert msg is not None and msg.author == "nick"


@pytest.mark.parametrize(
    "line",
    [
        "PING :tmi.twitch.tv",
        ":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!",
        ":justinfan1!justinfan1@justinfan1.tmi.twitch.tv JOIN #canal",
        "@badge-info=;color= :tmi.twitch.tv USERSTATE #canal",
        "",
    ],
)
def test_twitch_ignora_nao_privmsg(line):
    assert _parse_twitch_line(line) is None


# --- Kick -------------------------------------------------------------------
def _km(i: int, typ: str = "message") -> dict:
    return {
        "id": f"id{i}",
        "content": f"m{i}",
        "type": typ,
        "created_at": f"2026-11-17T12:00:{i:02d}Z",
        "sender": {"username": "u"},
    }


def _page(ids: list[int], cursor: str | None = "c", typ: str = "message") -> dict:
    # a API devolve as mensagens em ordem cronológica dentro da página; aqui embaralhamos
    return {"data": {"messages": [_km(i, typ) for i in ids], "cursor": cursor}}


def test_kick_primeira_pagina_em_ordem():
    pager = _KickPager(lambda cur: _page([3, 1, 2]))
    assert [m["id"] for m in pager.poll()] == ["id1", "id2", "id3"]


def test_kick_sobreposicao_so_novas():
    pages = iter([_page([1, 2, 3]), _page([2, 3, 4, 5])])
    pager = _KickPager(lambda cur: next(pages))
    pager.poll()
    assert [m["id"] for m in pager.poll()] == ["id4", "id5"]


def test_kick_backfill_ate_sobrepor():
    calls: list[str | None] = []
    data = {
        None: _page([10, 11, 12], cursor="p2"),
        "p2": _page([7, 8, 9], cursor="p3"),
        "p3": _page([3, 4, 5, 6], cursor="p4"),  # 3 já visto
        "p4": _page([1, 2], cursor=None),
    }
    first = _page([1, 2, 3])

    primed = []

    def fetch(cur):
        calls.append(cur)
        return data[cur] if primed else first

    pager = _KickPager(fetch)
    pager.poll()
    primed.append(True)
    calls.clear()
    out = pager.poll()
    assert [m["id"] for m in out] == [f"id{i}" for i in range(4, 13)]
    assert calls == [None, "p2", "p3"]


def test_kick_limite_de_paginas():
    calls: list[str | None] = []
    primed = []

    def fetch(cur):
        calls.append(cur)
        if not primed:
            return _page([1])
        n = len(calls)
        return _page([100 + n], cursor=f"c{n}")

    pager = _KickPager(fetch, max_backfill_pages=2)
    pager.poll()
    primed.append(True)
    calls.clear()
    pager.poll()
    assert len(calls) == 1 + 2  # página recente + 2 de backfill


def test_kick_ignora_tipo_nao_message_mas_dedupa():
    pages = iter([_page([1, 2]), _page([1, 2])])
    pager = _KickPager(lambda cur: next(pages))
    first = pager.poll()
    assert len(first) == 2
    pager2 = _KickPager(lambda cur: _page([1, 2], typ="reply_x"))
    assert pager2.poll() == []


def test_kick_seen_limit():
    pager = _KickPager(lambda cur: _page([1]), seen_limit=2)
    for i in range(5):
        pager._remember(f"x{i}")
    assert len(pager._seen) == 2


# --- YouTube ----------------------------------------------------------------
_YT_DATA = {
    "contents": {
        "liveChatRenderer": {
            "continuations": [{"invalidationContinuationData": {"continuation": "CONT0"}}]
        }
    }
}


@pytest.mark.parametrize("fmt", ['window["ytInitialData"] = {};', "var ytInitialData = {};"])
def test_extract_yt_config(fmt):
    html = (
        '<html><script>ytcfg.set({"INNERTUBE_CLIENT_VERSION":"2.20261117.01.00","X":1});</script>'
        f"<script>{fmt.format(json.dumps(_YT_DATA))}</script></html>"
    )
    # str.format não funciona com chaves de JSON: monta manualmente
    html = html.replace("{}", json.dumps(_YT_DATA), 1) if "{}" in html else html
    assert _extract_yt_config(html) == ("2.20261117.01.00", "CONT0")


def test_extract_yt_config_ausente():
    with pytest.raises(RuntimeError):
        _extract_yt_config("<html></html>")


def _yt_resp(continuation: dict | None) -> dict:
    live: dict = {
        "actions": [
            {
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "authorName": {"simpleText": "Zé"},
                            "message": {
                                "runs": [
                                    {"text": "oi "},
                                    {"emoji": {"shortcuts": [":fire:"], "emojiId": "x"}},
                                    {"text": " fim"},
                                ]
                            },
                            "timestampUsec": "1700000000500000",
                        }
                    }
                }
            },
            {"addChatItemAction": {"item": {"liveChatViewerEngagementMessageRenderer": {}}}},
            {"other": 1},
        ]
    }
    if continuation is not None:
        live["continuations"] = [continuation]
    return {"continuationContents": {"liveChatContinuation": live}}


def test_parse_yt_invalidation():
    msgs, cont, timeout = _parse_yt_chat_response(
        _yt_resp({"invalidationContinuationData": {"continuation": "N1", "timeoutMs": 3000}})
    )
    assert msgs == [ChatMessage("youtube", "Zé", "oi :fire: fim", 1700000000.5)]
    assert cont == "N1"
    assert timeout == 3.0


def test_parse_yt_timed():
    _, cont, timeout = _parse_yt_chat_response(
        _yt_resp({"timedContinuationData": {"continuation": "N2", "timeoutMs": 1000}})
    )
    assert (cont, timeout) == ("N2", 1.0)


def test_parse_yt_sem_continuation():
    msgs, cont, _ = _parse_yt_chat_response(_yt_resp(None))
    assert len(msgs) == 1 and cont is None


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.text, self.status_code = text, status


class _Http:
    def __init__(self, text: str):
        self.text = text
        self.urls: list[str] = []

    def get(self, url, **kw):
        self.urls.append(url)
        return _Resp(self.text)


def test_resolve_video_id_direto_e_canonical():
    http = _Http("")
    assert _resolve_youtube_video_id(http, "https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    assert _resolve_youtube_video_id(http, "https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert http.urls == []
    live = _Http('<link rel="canonical" href="https://www.youtube.com/watch?v=ZZZZZZZZZZZ">')
    assert _resolve_youtube_video_id(live, "https://www.youtube.com/@canal") == "ZZZZZZZZZZZ"
    assert live.urls == ["https://www.youtube.com/@canal/live"]


def test_resolve_video_id_offline():
    off = _Http('<link rel="canonical" href="https://www.youtube.com/@canal">')
    with pytest.raises(RuntimeError, match="não está ao vivo"):
        _resolve_youtube_video_id(off, "https://www.youtube.com/@canal")


# --- fábrica ----------------------------------------------------------------
def test_make_chat_source():
    assert isinstance(make_chat_source("https://twitch.tv/Foo"), TwitchIRCSource)
    assert make_chat_source("https://twitch.tv/Foo").channel == "foo"
    assert isinstance(make_chat_source("https://kick.com/bar"), KickHistorySource)
    assert isinstance(make_chat_source("https://youtube.com/@x"), YouTubeInnerTubeSource)


# --- ChatListener -----------------------------------------------------------
class _FakeSource:
    def __init__(self, stop: threading.Event):
        self.stop = stop
        self.calls = 0

    def messages(self, stop_event):
        self.calls += 1
        if self.calls == 1:
            for i in range(3):
                yield ChatMessage("twitch", "a", f"m{i}", float(i))
            raise ConnectionError("caiu")
        for i in range(3, 5):
            yield ChatMessage("twitch", "a", f"m{i}", float(i))
        self.stop.set()


def test_chat_listener_reconecta(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(chat_listener, "_INITIAL_BACKOFF_S", 0.01)
    monkeypatch.setattr(chat_listener.config, "CHAT_RECONNECT_MAX_BACKOFF_S", 0.05)
    session = Session(root=tmp_path, url="https://twitch.tv/x", platform="twitch",
                      channel="x", created_at=now())
    stop = threading.Event()
    listener = ChatListener(session, _FakeSource(stop), stop)
    thread = threading.Thread(target=listener.run)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    lines = session.chat_path.read_text(encoding="utf-8").splitlines()
    assert [ChatMessage.from_json(l).text for l in lines] == [f"m{i}" for i in range(5)]
    assert listener.message_count == 5
    assert listener.reconnect_count == 1


def test_chat_listener_fonte_que_retorna_sem_stop_reconecta(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(chat_listener, "_INITIAL_BACKOFF_S", 0.01)
    session = Session(root=tmp_path, url="u", platform="youtube", channel="x", created_at=now())
    stop = threading.Event()

    class Src:
        n = 0

        def messages(self, stop_event):
            Src.n += 1
            if Src.n >= 3:
                stop.set()
            return iter(())

    listener = ChatListener(session, Src(), stop)
    listener.run()
    assert listener.reconnect_count == 2
