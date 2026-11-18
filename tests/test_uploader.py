"""Testes do uploader (ADR-012): sem rede e sem Google real (sessão HTTP falsa injetada)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import config
import uploader
from uploader import (AuthError, QuotaError, UploadError, UploadEvent, UploadQueue, UploadRecord,
                      YouTubeClient)

CHUNK = 100
FINAL_JSON = {"id": "abc", "status": {"privacyStatus": "public"}}


# ───────────────────────────────── falsos ─────────────────────────────────
class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, body: object = None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("sem corpo")
        return self._body


def error_response(status: int, reason: str = "", message: str = "falhou") -> FakeResponse:
    return FakeResponse(status, body={"error": {"code": status, "message": message,
                                                "errors": [{"reason": reason}]}})


class FakeSession:
    """Servidor de upload retomável em memória.

    `put_script`: {n-ésimo PUT (0-based): resposta ou exceção} injetadas ANTES de processar.
    `accept_limit`: máximo de bytes aceitos por PUT (simula 308 parcial).
    """

    def __init__(self, *, start: FakeResponse | None = None, put_script: dict | None = None,
                 accept_limit: int | None = None, final: dict | None = None) -> None:
        self.start = start or FakeResponse(200, {"Location": "https://upload.example/session1"})
        self.put_script = put_script or {}
        self.accept_limit = accept_limit
        self.final = final if final is not None else FINAL_JSON
        self.received = bytearray()
        self.post_calls: list[dict] = []
        self.put_headers: list[dict] = []
        self.put_count = 0

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "params": params, "json": json, "headers": headers})
        if isinstance(self.start, Exception):
            raise self.start
        return self.start

    def put(self, url, data=b"", headers=None, timeout=None):
        index = self.put_count
        self.put_count += 1
        self.put_headers.append(dict(headers or {}))
        injected = self.put_script.get(index)
        if isinstance(injected, Exception):
            raise injected
        if injected is not None:
            return injected
        rng = (headers or {})["Content-Range"]
        spec, total_text = rng.split(" ")[1].split("/")
        total = int(total_text)
        if spec != "*":
            start, _ = (int(x) for x in spec.split("-"))
            assert start == len(self.received), "cliente enviou do offset errado"
            payload = bytes(data)
            if self.accept_limit is not None:
                payload = payload[: self.accept_limit]
            self.received.extend(payload)
        if len(self.received) >= total:
            return FakeResponse(200, body=self.final)
        if not self.received:
            return FakeResponse(308)
        return FakeResponse(308, {"Range": f"bytes=0-{len(self.received) - 1}"})


class FakeCreds:
    valid = True
    refresh_token = "refresh"

    def to_json(self) -> str:
        return "{}"


def make_client(tmp_path: Path, session: object, creds: object | None = None) -> YouTubeClient:
    client = YouTubeClient(tmp_path / "secret.json", tmp_path / "token.json",
                           session_factory=lambda _creds: session,
                           credentials_loader=lambda: creds if creds is not None else FakeCreds())
    client._sleep = lambda _s: None
    return client


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_CHUNK_BYTES", CHUNK)


@pytest.fixture
def clip(tmp_path) -> Path:
    path = tmp_path / "sessao" / "final" / "clip_03_seg0_12m30s.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(bytes(range(256)) + bytes(range(44)))     # 300 bytes = 3 blocos
    return path


def meta_for(clip: Path, **kw) -> uploader.VideoMeta:
    return uploader.build_meta(clip, channel="gaules", platform="twitch", **kw)


# ───────────────────────────── caminhos e metadados ───────────────────────────
def test_paths_use_data_dir():
    import runtime
    assert uploader.client_secret_path() == runtime.data_dir() / config.YOUTUBE_CLIENT_SECRET_FILE
    assert uploader.token_path() == runtime.data_dir() / config.YOUTUBE_TOKEN_FILE


def test_clip_index():
    assert uploader.clip_index(Path("x/clip_03_seg0_12m30s.mp4")) == 3
    assert uploader.clip_index(Path("clip_12.mp4")) == 12
    assert uploader.clip_index(Path("video.mp4")) is None


def test_build_meta_templates(clip):
    meta = meta_for(clip, when=time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1)))
    assert meta.title == "gaules | momento #3 da live #Shorts"
    assert "gaules" in meta.description and "Twitch" in meta.description
    assert "18/11/2026" in meta.description
    assert meta.privacy == config.YOUTUBE_PRIVACY
    assert meta.category_id == config.YOUTUBE_CATEGORY_ID
    other = uploader.build_meta(Path("video.mp4"), channel="x", platform="kick", when=0)
    assert "momento #1" in other.title and "Kick" in other.description


def test_build_meta_truncates_and_cleans(clip):
    meta = meta_for(clip, title="  <b>" + "a" * 200 + "</b>  ")
    assert len(meta.title) == 100 and "<" not in meta.title and ">" not in meta.title
    assert meta.title.startswith("ba")
    spaced = meta_for(clip, title="um   dois\t tres")
    assert spaced.title == "um dois tres"


def test_build_meta_description_limit(clip, monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_DESCRIPTION_TEMPLATE", "<x>" + "y" * 6000)
    meta = meta_for(clip)
    assert len(meta.description) == 5000 and "<" not in meta.description
    monkeypatch.setattr(config, "YOUTUBE_DESCRIPTION_TEMPLATE", "é" * 4000)   # 8000 bytes
    assert len(meta_for(clip).description.encode("utf-8")) <= 5000


def test_build_meta_title_empty_falls_back_to_filename(clip):
    assert meta_for(clip, title="<>").title == clip.stem
    assert meta_for(clip, title="   ").title == clip.stem


def test_build_meta_privacy(clip):
    assert meta_for(clip, privacy="unlisted").privacy == "unlisted"
    with pytest.raises(ValueError):
        meta_for(clip, privacy="secreto")


def test_build_meta_tags_without_duplicates(clip, monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_TAGS", ("shorts", "Gaules", "shorts"))
    meta = meta_for(clip)
    assert meta.tags == ("shorts", "Gaules")
    monkeypatch.setattr(config, "YOUTUBE_TAGS", ("shorts",))
    assert meta_for(clip).tags == ("shorts", "gaules")


def test_build_meta_bad_template_does_not_crash(clip, monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_TITLE_TEMPLATE", "{canal_inexistente}")
    assert meta_for(clip).title == "{canal_inexistente}"


# ───────────────────────────────── registros ──────────────────────────────────
def record(name: str = "clip_01.mp4", vid: str = "v1") -> UploadRecord:
    return UploadRecord(name, vid, uploader.WATCH_URL.format(video_id=vid), "public", 1000.0)


def test_records_roundtrip_and_atomic(tmp_path):
    assert uploader.load_records(tmp_path) == {}
    uploader.save_record(tmp_path, record("clip_01.mp4", "v1"))
    uploader.save_record(tmp_path, record("clip_02.mp4", "v2"))
    uploader.save_record(tmp_path, record("clip_01.mp4", "v9"))      # substitui
    loaded = uploader.load_records(tmp_path)
    assert set(loaded) == {"clip_01.mp4", "clip_02.mp4"}
    assert loaded["clip_01.mp4"].video_id == "v9"
    assert uploader.records_path(tmp_path).name == "youtube.json"
    assert not list(tmp_path.glob("*.tmp"))


def test_records_corrupted_returns_empty_and_recovers(tmp_path):
    uploader.records_path(tmp_path).write_text("{ nada", encoding="utf-8")
    assert uploader.load_records(tmp_path) == {}
    uploader.records_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    assert uploader.load_records(tmp_path) == {}
    uploader.save_record(tmp_path, record())          # sobrescreve o arquivo ruim
    assert "clip_01.mp4" in uploader.load_records(tmp_path)


def test_is_posted(tmp_path):
    clip = tmp_path / "sessao" / "final" / "clip_01.mp4"
    clip.parent.mkdir(parents=True)
    assert uploader.is_posted(clip) is None
    uploader.save_record(tmp_path / "sessao", record())
    found = uploader.is_posted(clip)
    assert found is not None and found.video_id == "v1"
    assert uploader.is_posted(clip, tmp_path / "sessao") == found
    assert uploader.is_posted(clip.with_name("clip_02.mp4")) is None


# ─────────────────────────────── credencial do app ────────────────────────────
def test_install_client_secret_installed_ok(tmp_path):
    source = tmp_path / "baixado.json"
    source.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
    client = YouTubeClient(tmp_path / "dados" / "secret.json", tmp_path / "token.json")
    assert not client.is_configured()
    client.install_client_secret(source)
    assert client.is_configured()
    assert (tmp_path / "dados" / "secret.json").read_bytes() == source.read_bytes()


def test_install_client_secret_rejects_web_invalid_and_incomplete(tmp_path):
    client = YouTubeClient(tmp_path / "secret.json", tmp_path / "token.json")
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
    with pytest.raises(AuthError, match="App para computador"):
        client.install_client_secret(web)
    bad = tmp_path / "ruim.json"
    bad.write_text("não é json", encoding="utf-8")
    with pytest.raises(AuthError, match="JSON"):
        client.install_client_secret(bad)
    incomplete = tmp_path / "incompleto.json"
    incomplete.write_text(json.dumps({"installed": {"client_id": "a"}}), encoding="utf-8")
    with pytest.raises(AuthError):
        client.install_client_secret(incomplete)
    with pytest.raises(AuthError):
        client.install_client_secret(tmp_path / "nao_existe.json")
    assert not client.is_configured()


def test_install_new_client_secret_drops_old_token(tmp_path):
    client = YouTubeClient(tmp_path / "secret.json", tmp_path / "token.json")
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    first = tmp_path / "a.json"
    first.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
    client.install_client_secret(first)
    assert client.is_connected()          # primeira instalação não mexe no token
    second = tmp_path / "b.json"
    second.write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "d"}}), encoding="utf-8")
    client.install_client_secret(second)
    assert not client.is_connected()


def test_is_connected_and_disconnect(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    client = YouTubeClient(tmp_path / "secret.json", token)
    assert not client.is_connected()
    token.write_text(json.dumps({"token": "t"}), encoding="utf-8")
    assert not client.is_connected()                 # sem refresh_token
    token.write_text(json.dumps({"token": "t", "refresh_token": "r"}), encoding="utf-8")
    assert client.is_connected()
    revoked = []
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: revoked.append((url, kw["params"])))
    client.disconnect()
    assert not token.exists() and not client.is_connected()
    assert revoked == [(uploader.REVOKE_URL, {"token": "r"})]


def test_disconnect_survives_revoke_failure(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("sem rede")

    monkeypatch.setattr(requests, "post", boom)
    YouTubeClient(tmp_path / "s.json", token).disconnect()
    assert not token.exists()


def test_connect_without_client_secret(tmp_path):
    pytest.importorskip("google_auth_oauthlib")
    with pytest.raises(AuthError):
        YouTubeClient(tmp_path / "s.json", tmp_path / "t.json").connect()


def test_connect_saves_token_and_passes_messages(tmp_path, monkeypatch):
    flow_module = pytest.importorskip("google_auth_oauthlib.flow")
    secret = tmp_path / "s.json"
    secret.write_text("{}", encoding="utf-8")
    seen = {}

    class Creds:
        refresh_token = "r"

        def to_json(self):
            return json.dumps({"refresh_token": "r"})

    class Flow:
        def run_local_server(self, **kwargs):
            seen.update(kwargs)
            return Creds()

    monkeypatch.setattr(flow_module.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, path, scopes: Flow()))
    client = YouTubeClient(secret, tmp_path / "t.json")
    client.connect(open_browser=False)
    assert client.is_connected()
    assert seen["port"] == 0 and seen["open_browser"] is False
    assert seen["timeout_seconds"] == config.YOUTUBE_LOGIN_TIMEOUT_S
    assert "Login concluído" in seen["success_message"]
    assert "navegador" in seen["authorization_prompt_message"]


def test_connect_failure_becomes_auth_error(tmp_path, monkeypatch):
    flow_module = pytest.importorskip("google_auth_oauthlib.flow")
    secret = tmp_path / "s.json"
    secret.write_text("{}", encoding="utf-8")

    class Flow:
        def run_local_server(self, **kwargs):
            raise RuntimeError("usuário fechou")

    monkeypatch.setattr(flow_module.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, path, scopes: Flow()))
    with pytest.raises(AuthError):
        YouTubeClient(secret, tmp_path / "t.json").connect()


def test_missing_google_package_gives_auth_error(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError("sem google")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    client = YouTubeClient(tmp_path / "s.json", tmp_path / "t.json")
    with pytest.raises(AuthError, match="Componente de login do Google ausente"):
        client.connect()
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    with pytest.raises(AuthError, match="Componente de login do Google ausente"):
        client.upload(clip, uploader.build_meta(clip, channel="c", platform="twitch"))


# ───────────────────────────────────── upload ─────────────────────────────────
def test_upload_happy_path_multiple_chunks(tmp_path, clip):
    session = FakeSession()
    client = make_client(tmp_path, session)
    progress: list[tuple[int, int]] = []
    meta = meta_for(clip, privacy="unlisted")
    rec = client.upload(clip, meta, on_progress=lambda d, t: progress.append((d, t)))
    assert bytes(session.received) == clip.read_bytes()
    assert [h["Content-Range"] for h in session.put_headers] == [
        "bytes 0-99/300", "bytes 100-199/300", "bytes 200-299/300"]
    assert progress == [(100, 300), (200, 300), (300, 300)]
    assert rec.clip_name == clip.name and rec.video_id == "abc"
    assert rec.url == "https://youtube.com/shorts/abc" and rec.privacy == "public"
    call = session.post_calls[0]
    assert call["url"] == uploader.UPLOAD_URL
    assert call["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    assert call["headers"] == {"X-Upload-Content-Length": "300", "X-Upload-Content-Type": "video/mp4"}
    body = call["json"]
    assert body["snippet"]["title"] == meta.title and body["snippet"]["categoryId"] == meta.category_id
    assert body["snippet"]["tags"] == list(meta.tags)
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}


def test_upload_partial_308_resumes_from_range(tmp_path, clip):
    session = FakeSession(accept_limit=60)
    client = make_client(tmp_path, session)
    client.upload(clip, meta_for(clip))
    assert bytes(session.received) == clip.read_bytes()
    starts = [h["Content-Range"].split(" ")[1].split("-")[0] for h in session.put_headers]
    assert starts[:3] == ["0", "60", "120"]


def test_upload_308_without_range_restarts_at_zero(tmp_path, clip):
    session = FakeSession(put_script={0: FakeResponse(308)})
    make_client(tmp_path, session).upload(clip, meta_for(clip))
    assert bytes(session.received) == clip.read_bytes()
    assert session.put_headers[0]["Content-Range"] == "bytes 0-99/300"
    assert session.put_headers[1]["Content-Range"] == "bytes 0-99/300"


def test_upload_retries_after_503_querying_state(tmp_path, clip):
    session = FakeSession(put_script={1: error_response(503)})
    client = make_client(tmp_path, session)
    sleeps: list[float] = []
    client._sleep = sleeps.append
    rec = client.upload(clip, meta_for(clip))
    assert rec.video_id == "abc"
    assert bytes(session.received) == clip.read_bytes()
    ranges = [h["Content-Range"] for h in session.put_headers]
    assert ranges[:3] == ["bytes 0-99/300", "bytes 100-199/300", "bytes */300"]
    assert ranges[3] == "bytes 100-199/300"
    assert sleeps == [config.YOUTUBE_RETRY_BASE_S]


def test_upload_retries_after_network_error_with_backoff(tmp_path, clip):
    session = FakeSession(put_script={0: ConnectionError("caiu"), 1: error_response(502),
                                      2: ConnectionError("caiu de novo")})
    client = make_client(tmp_path, session)
    sleeps: list[float] = []
    client._sleep = sleeps.append
    client.upload(clip, meta_for(clip))
    assert bytes(session.received) == clip.read_bytes()
    base = config.YOUTUBE_RETRY_BASE_S
    assert sleeps == [base, base * 2, base * 4]


def test_upload_gives_up_after_max_retries(tmp_path, clip):
    session = FakeSession(put_script={i: error_response(503) for i in range(100)})
    client = make_client(tmp_path, session)
    sleeps: list[float] = []
    client._sleep = sleeps.append
    with pytest.raises(UploadError, match="falhou várias vezes"):
        client.upload(clip, meta_for(clip))
    assert len(sleeps) == config.YOUTUBE_MAX_RETRIES


def test_upload_start_retries_on_503(tmp_path, clip):
    calls = {"n": 0}
    session = FakeSession()
    original = session.post

    def flaky_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return error_response(503)
        return original(*a, **k)

    session.post = flaky_post
    make_client(tmp_path, session).upload(clip, meta_for(clip))
    assert calls["n"] == 2


@pytest.mark.parametrize("reason", ["quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"])
def test_upload_quota_error(tmp_path, clip, reason):
    session = FakeSession(start=error_response(403, reason, "cota"))
    with pytest.raises(QuotaError):
        make_client(tmp_path, session).upload(clip, meta_for(clip))


def test_upload_quota_error_mid_upload(tmp_path, clip):
    session = FakeSession(put_script={1: error_response(403, "quotaExceeded")})
    with pytest.raises(QuotaError):
        make_client(tmp_path, session).upload(clip, meta_for(clip))


def test_upload_other_403_is_upload_error_with_message(tmp_path, clip):
    session = FakeSession(start=error_response(403, "forbidden", "Sem permissão no canal"))
    with pytest.raises(UploadError, match="Sem permissão no canal") as info:
        make_client(tmp_path, session).upload(clip, meta_for(clip))
    assert not isinstance(info.value, (QuotaError, AuthError))


def test_upload_401_deletes_token_and_raises_auth(tmp_path, clip):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    session = FakeSession(put_script={0: error_response(401, "authError")})
    with pytest.raises(AuthError, match="conecte de novo em Ajustes"):
        make_client(tmp_path, session).upload(clip, meta_for(clip))
    assert not token.exists()


def test_upload_refresh_error_is_auth_error(tmp_path, clip):
    exceptions = pytest.importorskip("google.auth.exceptions")

    class Expired:
        valid = False
        refresh_token = "r"

        def refresh(self, request):
            raise exceptions.RefreshError("invalid_grant")

    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    with pytest.raises(AuthError, match="expirou"):
        make_client(tmp_path, FakeSession(), Expired()).upload(clip, meta_for(clip))
    assert not token.exists()


def test_upload_refreshes_expired_credentials(tmp_path, clip):
    pytest.importorskip("google.auth.transport.requests")

    class Expired:
        valid = False
        refresh_token = "r"
        refreshed = False

        def refresh(self, request):
            self.refreshed = True
            self.valid = True

    creds = Expired()
    make_client(tmp_path, FakeSession(), creds).upload(clip, meta_for(clip))
    assert creds.refreshed


def test_upload_without_token_is_auth_error(tmp_path, clip):
    pytest.importorskip("google.oauth2.credentials")
    client = YouTubeClient(tmp_path / "s.json", tmp_path / "nao_existe.json",
                           session_factory=lambda c: FakeSession())
    with pytest.raises(AuthError, match="não conectado"):
        client.upload(clip, meta_for(clip))


def test_upload_stop_event_cancels_between_chunks(tmp_path, clip):
    session = FakeSession()
    stop = threading.Event()
    client = make_client(tmp_path, session)

    def progress(done, total):
        if done >= 100:
            stop.set()

    with pytest.raises(UploadError, match="cancelado"):
        client.upload(clip, meta_for(clip), on_progress=progress, stop_event=stop)
    assert session.put_count == 1


def test_upload_missing_or_empty_file(tmp_path):
    client = make_client(tmp_path, FakeSession())
    gone = tmp_path / "nada.mp4"
    meta = uploader.build_meta(gone, channel="c", platform="twitch")
    with pytest.raises(UploadError):
        client.upload(gone, meta)
    empty = tmp_path / "vazio.mp4"
    empty.write_bytes(b"")
    with pytest.raises(UploadError):
        client.upload(empty, meta)


def test_upload_returns_privacy_returned_by_youtube(tmp_path, clip):
    session = FakeSession(final={"id": "zzz", "status": {"privacyStatus": "private"}})
    rec = make_client(tmp_path, session).upload(clip, meta_for(clip, privacy="public"))
    assert rec.privacy == "private"


def test_upload_start_without_location_fails(tmp_path, clip):
    session = FakeSession(start=FakeResponse(200, {}))
    with pytest.raises(UploadError):
        make_client(tmp_path, session).upload(clip, meta_for(clip))


def test_upload_does_not_read_whole_file_at_once(tmp_path, clip):
    session = FakeSession()
    sizes: list[int] = []
    original = session.put

    def spy(url, data=b"", headers=None, timeout=None):
        sizes.append(len(data))
        return original(url, data=data, headers=headers, timeout=timeout)

    session.put = spy
    make_client(tmp_path, session).upload(clip, meta_for(clip))
    assert max(sizes) == CHUNK


# ───────────────────────────────────── fila ───────────────────────────────────
class FakeClient:
    """Cliente falso: `script` mapeia nome do clipe → exceção a levantar (ou ausente = ok)."""

    def __init__(self, script: dict | None = None, gate: threading.Event | None = None) -> None:
        self.script = script or {}
        self.gate = gate
        self.uploaded: list[str] = []

    def upload(self, clip, meta, *, on_progress=None, stop_event=None):
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if stop_event is not None and stop_event.is_set():
                    raise UploadError("cancelado")
        if on_progress:
            on_progress(5, 10)
        exc = self.script.get(clip.name)
        if exc is not None:
            raise exc
        self.uploaded.append(clip.name)
        return UploadRecord(clip.name, "vid_" + clip.stem, uploader.WATCH_URL.format(video_id="vid_" + clip.stem),
                            "public", time.time())


class Collector:
    def __init__(self) -> None:
        self.events: list[UploadEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event: UploadEvent) -> None:
        with self._lock:
            self.events.append(event)

    def kinds(self, clip: Path | None = None) -> list[str]:
        with self._lock:
            return [e.kind for e in self.events if clip is None or e.clip == clip]

    def wait_for(self, predicate, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if predicate(list(self.events)):
                    return True
            time.sleep(0.01)
        return False


@pytest.fixture
def session_clips(tmp_path):
    folder = tmp_path / "sessao" / "final"
    folder.mkdir(parents=True)
    clips = []
    for i in range(1, 4):
        path = folder / f"clip_{i:02d}.mp4"
        path.write_bytes(b"x" * 50)
        clips.append(path)
    return tmp_path / "sessao", clips


def test_queue_done_writes_record_and_emits_events(session_clips):
    session_dir, clips = session_clips
    sink = Collector()
    queue = UploadQueue(FakeClient(), sink)
    try:
        assert queue.submit(clips[0], meta_for(clips[0]))
        assert sink.wait_for(lambda ev: any(e.kind == "done" for e in ev))
    finally:
        queue.shutdown(wait=True)
    assert sink.kinds(clips[0]) == ["queued", "started", "progress", "done"]
    done = next(e for e in sink.events if e.kind == "done")
    assert done.record is not None and done.record.video_id == "vid_clip_01"
    assert uploader.load_records(session_dir)["clip_01.mp4"].video_id == "vid_clip_01"
    assert (session_dir / "youtube.json").exists()


def test_queue_rejects_duplicates_and_already_posted(session_clips):
    session_dir, clips = session_clips
    gate = threading.Event()
    queue = UploadQueue(FakeClient(gate=gate), Collector())
    try:
        assert queue.submit(clips[0], meta_for(clips[0])) is True
        assert queue.submit(clips[0], meta_for(clips[0])) is False      # na fila/em envio
        assert queue.is_queued(clips[0]) and queue.pending() == 1
        assert queue.submit(clips[1], meta_for(clips[1])) is True
        assert queue.pending() == 2
        uploader.save_record(session_dir, record("clip_03.mp4", "ja"))
        assert queue.submit(clips[2], meta_for(clips[2])) is False      # já postado
        assert not queue.is_queued(clips[2])
    finally:
        gate.set()
        queue.shutdown(wait=True)


def test_queue_failure_of_one_does_not_stop_others(session_clips):
    _session_dir, clips = session_clips
    sink = Collector()
    queue = UploadQueue(FakeClient({"clip_01.mp4": UploadError("rede")}), sink)
    try:
        queue.submit(clips[0], meta_for(clips[0]))
        queue.submit(clips[1], meta_for(clips[1]))
        assert sink.wait_for(lambda ev: any(e.kind == "done" for e in ev))
    finally:
        queue.shutdown(wait=True)
    failed = [e for e in sink.events if e.kind == "failed"]
    assert len(failed) == 1 and failed[0].error_kind == "other" and "rede" in failed[0].error
    assert failed[0].clip == clips[0]
    assert not queue.is_queued(clips[0])


def test_queue_unexpected_exception_keeps_thread_alive(session_clips):
    _session_dir, clips = session_clips
    sink = Collector()
    queue = UploadQueue(FakeClient({"clip_01.mp4": RuntimeError("bug")}), sink)
    try:
        queue.submit(clips[0], meta_for(clips[0]))
        queue.submit(clips[1], meta_for(clips[1]))
        assert sink.wait_for(lambda ev: any(e.kind == "done" for e in ev))
    finally:
        queue.shutdown(wait=True)
    assert [e.error_kind for e in sink.events if e.kind == "failed"] == ["other"]


def test_queue_callback_exception_does_not_kill_thread(session_clips):
    _session_dir, clips = session_clips
    seen: list[str] = []

    def bad_sink(event):
        seen.append(event.kind)
        raise RuntimeError("GUI quebrou")

    queue = UploadQueue(FakeClient(), bad_sink)
    try:
        queue.submit(clips[0], meta_for(clips[0]))
        queue.submit(clips[1], meta_for(clips[1]))
        deadline = time.monotonic() + 5
        while seen.count("done") < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        queue.shutdown(wait=True)
    assert seen.count("done") == 2


def test_queue_quota_empties_queue_with_failed_events(session_clips):
    session_dir, clips = session_clips
    sink = Collector()
    gate = threading.Event()
    client = FakeClient({"clip_01.mp4": QuotaError("cota")}, gate=gate)
    queue = UploadQueue(client, sink)
    try:
        for clip in clips:
            assert queue.submit(clip, meta_for(clip))
        gate.set()
        assert sink.wait_for(lambda ev: sum(e.kind == "failed" for e in ev) == 3)
    finally:
        queue.shutdown(wait=True)
    failed = [e for e in sink.events if e.kind == "failed"]
    assert {e.clip for e in failed} == set(clips)
    assert all(e.error_kind == "quota" and "tente amanhã" in e.error for e in failed)
    assert client.uploaded == []
    assert queue.pending() == 0
    assert uploader.load_records(session_dir) == {}


def test_queue_auth_empties_queue_with_failed_events(session_clips):
    _session_dir, clips = session_clips
    sink = Collector()
    gate = threading.Event()
    queue = UploadQueue(FakeClient({"clip_01.mp4": AuthError("Login do YouTube expirou")}, gate=gate), sink)
    try:
        for clip in clips[:2]:
            queue.submit(clip, meta_for(clip))
        gate.set()
        assert sink.wait_for(lambda ev: sum(e.kind == "failed" for e in ev) == 2)
    finally:
        queue.shutdown(wait=True)
    assert all(e.error_kind == "auth" for e in sink.events if e.kind == "failed")
    assert queue.pending() == 0


def test_queue_shutdown_cancels_current_and_discards_rest(session_clips):
    _session_dir, clips = session_clips
    sink = Collector()
    gate = threading.Event()               # nunca aberto: o envio fica "preso" até cancelar
    queue = UploadQueue(FakeClient(gate=gate), sink)
    queue.submit(clips[0], meta_for(clips[0]))
    queue.submit(clips[1], meta_for(clips[1]))
    assert sink.wait_for(lambda ev: any(e.kind == "started" for e in ev))
    started = time.monotonic()
    queue.shutdown(wait=True, timeout_s=5)
    assert time.monotonic() - started < 5
    assert queue.pending() == 0
    assert queue._thread is not None and not queue._thread.is_alive()
    assert queue.submit(clips[2], meta_for(clips[2])) is False     # fila encerrada
