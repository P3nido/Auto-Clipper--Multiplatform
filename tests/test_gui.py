"""Testes da interface gráfica, em modo offscreen e com FakeJobManager."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import dataclasses
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import config
import gui
from engine import JobState


@pytest.fixture(scope="module")
def app():
    return gui.create_app([])


@pytest.fixture(autouse=True)
def restore_config():
    saved = (config.OUTPUT_LAYOUT, config.WHISPER_LANGUAGE, config.SPIKE_MULTIPLIER, config.SESSIONS_DIR,
             config.EXPORT_DIR, config.YOUTUBE_PRIVACY, config.YOUTUBE_AUTO_UPLOAD)
    yield
    (config.OUTPUT_LAYOUT, config.WHISPER_LANGUAGE, config.SPIKE_MULTIPLIER, config.SESSIONS_DIR,
     config.EXPORT_DIR, config.YOUTUBE_PRIVACY, config.YOUTUBE_AUTO_UPLOAD) = saved


@pytest.fixture
def window(app, tmp_path):
    win = gui.MainWindow(
        lambda cb: gui.FakeJobManager(cb),
        settings_path=tmp_path / "settings.json",
        hardware_probe=lambda: "CPU",
        check_ffmpeg=lambda: True,
    )
    win.show()
    yield win
    win.manager.shutdown()
    win._disk_timer.stop()


def test_empty_state_then_cards(window):
    assert window.empty_state.isVisibleTo(window)
    assert not window.scroll.isVisibleTo(window)
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    assert len(window._cards) == 1
    assert not window.empty_state.isVisibleTo(window)
    assert window.scroll.isVisibleTo(window)
    assert window.url_edit.text() == ""


def test_invalid_link_shows_inline_message(window):
    window.url_edit.setText("https://example.com/algo")
    window.add_live()
    assert window.error_label.isVisibleTo(window)
    assert "Twitch, Kick ou YouTube" in window.error_label.text()
    assert not window._cards
    window.url_edit.setText("")
    window.add_live()
    assert "Cole o link" in window.error_label.text()


def test_link_without_scheme_and_duplicate(window):
    window.url_edit.setText("kick.com/casimito")
    window.add_live()
    assert len(window._cards) == 1
    window.url_edit.setText("https://kick.com/casimito")
    window.add_live()
    assert "já está" in window.error_label.text()
    assert len(window._cards) == 1


def test_status_updates_drive_card(window):
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    job_id = next(iter(window._cards))
    card = window._cards[job_id]
    assert card.state_label.text() == "Aguardando a live"
    assert card.stop_button.isVisibleTo(window)
    assert not card.remove_button.isVisibleTo(window)

    window.manager.update(job_id, state=JobState.RECORDING, message="Gravando há 0:10",
                          recorded_s=3725, recorded_bytes=3_221_225_472, chat_messages=12480, spikes=7)
    assert card.state_label.text() == "● AO VIVO"
    assert card.time_value.text() == "1:02:05"
    assert card.stats["size"].value.text() == "3,0 GB"
    assert card.stats["chat"].value.text() == "12.480"
    assert card.stats["spikes"].value.text() == "7"
    assert card.property("state") == "recording"

    window.manager.update(job_id, state=JobState.PROCESSING, clips_done=2, clips_total=8)
    assert "clipe 3 de 8" in card.state_label.text()
    assert card.progress.isVisibleTo(window)
    assert (card.progress.maximum(), card.progress.value()) == (8, 2)
    assert not card.stop_button.isVisibleTo(window)

    window.manager.update(job_id, state=JobState.ERROR, error="Streamlink falhou · tente de novo")
    assert card.message_label.text() == "Streamlink falhou, tente de novo"
    assert card.remove_button.isVisibleTo(window)


def test_open_clips_enabled_only_with_clips(window, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(gui, "open_folder", opened.append)
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    job_id = next(iter(window._cards))
    card = window._cards[job_id]
    window.manager.update(job_id, state=JobState.PROCESSING, clips_total=3)
    assert not card.open_button.isEnabled()
    clip = tmp_path / "final" / "clip_01.mp4"
    window.manager.update(job_id, state=JobState.DONE, clips_done=3, clips_total=3, final_clips=(clip,))
    assert card.open_button.isEnabled()
    card.open_button.click()
    assert opened == [clip.parent]


def test_recording_card_shows_live_clips_and_enables_open(window, tmp_path):
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    job_id = next(iter(window._cards))
    card = window._cards[job_id]
    window.manager.update(job_id, state=JobState.RECORDING, recorded_s=600, spikes=2, clips_total=2)
    assert card.stats["clips"].value.text() == "0 de 2"
    assert not card.stats["clips"].isHidden()
    assert not card.open_button.isEnabled()
    clip = tmp_path / "final" / "clip_01.mp4"
    window.manager.update(job_id, clips_done=1, final_clips=(clip,))
    assert card.stats["clips"].value.text() == "1 de 2"
    assert card.open_button.isEnabled() and card.stop_button.isVisibleTo(window)


def test_stop_asks_confirmation(window, monkeypatch):
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    job_id = next(iter(window._cards))
    asked = []
    monkeypatch.setattr(window, "confirm", lambda title, text: asked.append(text) or False)
    window._cards[job_id].stop_button.click()
    assert asked and "vira clipes" in asked[0]
    assert window.manager.status(job_id).state == JobState.WAITING
    monkeypatch.setattr(window, "confirm", lambda title, text: True)
    window._cards[job_id].stop_button.click()
    assert window.manager.status(job_id).state == JobState.STOPPED


def test_remove_finished_card(window):
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    job_id = next(iter(window._cards))
    window.manager.update(job_id, state=JobState.DONE)
    window._cards[job_id].remove_button.click()
    assert not window._cards
    assert window.empty_state.isVisibleTo(window)


def test_footer_counts_and_disk_warning(window, monkeypatch):
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    assert f"1 de {config.MAX_PARALLEL_LIVES}" in window.lives_label.text()
    monkeypatch.setattr(gui, "free_disk_gb", lambda path: 5.0)
    window.refresh_footer()
    assert "Pouco espaço" in window.disk_label.text()
    monkeypatch.setattr(gui, "free_disk_gb", lambda path: 300.0)
    window.refresh_footer()
    assert "300 GB" in window.disk_label.text()


def test_hardware_label_filled_from_thread(window, app):
    import time
    deadline = time.monotonic() + 3
    while "Detectando" in window.hardware_label.text() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.hardware_label.text() == "Encoder: CPU"


def test_hardware_failure_falls_back_to_cpu(app, tmp_path):
    def boom():
        raise RuntimeError("sem ffmpeg")

    win = gui.MainWindow(lambda cb: gui.FakeJobManager(cb), settings_path=tmp_path / "s.json",
                         hardware_probe=boom, check_ffmpeg=lambda: True)
    import time
    deadline = time.monotonic() + 3
    while "Detectando" in win.hardware_label.text() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert win.hardware_label.text() == "Encoder: CPU"
    win._disk_timer.stop()


def test_ffmpeg_banner(app, tmp_path):
    win = gui.MainWindow(lambda cb: gui.FakeJobManager(cb), settings_path=tmp_path / "s.json",
                         hardware_probe=lambda: "CPU", check_ffmpeg=lambda: False)
    win.show()
    assert win.ffmpeg_banner.isVisibleTo(win)
    assert "winget install Gyan.FFmpeg" in win.ffmpeg_banner.text()
    win._disk_timer.stop()


def test_settings_roundtrip_and_apply(tmp_path):
    path = tmp_path / "settings.json"
    assert gui.load_settings(path) == gui.default_settings()
    values = {"output_layout": "horizontal", "whisper_language": None,
              "spike_multiplier": 2.2, "sessions_dir": str(tmp_path / "saida"),
              "export_dir": str(tmp_path / "drive"),
              "youtube_privacy": "unlisted", "youtube_auto_upload": True}
    gui.save_settings(values, path)
    assert json.loads(path.read_text(encoding="utf-8"))["output_layout"] == "horizontal"
    loaded = gui.load_settings(path)
    assert loaded == values
    gui.apply_settings(loaded)
    assert config.OUTPUT_LAYOUT == "horizontal"
    assert config.WHISPER_LANGUAGE is None
    assert config.SPIKE_MULTIPLIER == 2.2
    assert config.SESSIONS_DIR == str(tmp_path / "saida")
    assert config.EXPORT_DIR == str(tmp_path / "drive")
    assert config.YOUTUBE_PRIVACY == "unlisted"
    assert config.YOUTUBE_AUTO_UPLOAD is True


def test_export_dir_default_is_auto():
    assert gui.default_settings()["export_dir"] == "auto"


def test_corrupt_settings_fall_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ isso não é json", encoding="utf-8")
    assert gui.load_settings(path) == gui.default_settings()


def test_settings_dialog_values_and_window_persistence(window, app, tmp_path):
    dialog = gui.SettingsDialog(window._settings)
    dialog.layout_combo.setCurrentIndex(dialog.layout_combo.findData("vertical_crop"))
    dialog.language_combo.setCurrentIndex(dialog.language_combo.findData(None))
    dialog.sensitivity_combo.setCurrentIndex(dialog.sensitivity_combo.findData(2.2))
    dialog.dir_edit.setText(str(tmp_path / "clips"))
    window.apply_new_settings(dialog.values())
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["output_layout"] == "vertical_crop"
    assert saved["whisper_language"] is None
    assert saved["spike_multiplier"] == 2.2
    assert config.SPIKE_MULTIPLIER == 2.2
    assert config.SESSIONS_DIR == str(tmp_path / "clips")


def test_demo_manager_seed_and_ticks(app, tmp_path):
    seen = []
    manager = gui.FakeJobManager(seen.append, seed=True, clips_dir=tmp_path)
    assert [j.state for j in manager.all()] == [JobState.RECORDING, JobState.PROCESSING, JobState.WAITING]
    for _ in range(18):
        manager._tick()
    states = {j.channel: j.state for j in manager.all()}
    assert states["casimito"] == JobState.DONE
    assert manager.status(manager.all()[0].job_id).recorded_s > 4350


def test_close_with_active_lives_asks_and_shuts_down(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    monkeypatch.setattr(window, "confirm", lambda title, text: False)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()

    called = []
    window.manager.shutdown = lambda wait=True: called.append(wait)
    monkeypatch.setattr(window, "confirm", lambda title, text: True)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert called == [True]


# ───────────────────────────── envio ao Drive (ADR-011) ─────────────────────────
import time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

import exporter


def _make_clips(folder: Path, n: int = 3) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    for i in range(1, n + 1):
        clip = folder / f"clip_{i:02d}.mp4"
        clip.write_bytes(b"x" * (1000 * i))
        clips.append(clip)
    return clips


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QTest.qWait(20)
        if predicate():
            return True
    return predicate()


def test_settings_dialog_export_dir(app, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "detect_drive_folder", lambda: None)
    dialog = gui.SettingsDialog(gui.default_settings())
    assert dialog.values()["export_dir"] == "auto"
    assert "não encontrado" in dialog.export_label.text()
    monkeypatch.setattr(exporter, "detect_drive_folder", lambda: tmp_path / "Meu Drive")
    dialog._detect_export()
    assert dialog.export_label.text().startswith("Detectado:")
    dialog.export_edit.setText(str(tmp_path / "x"))
    assert dialog.export_label.text().startswith("Os clipes vão para:")
    assert dialog.values()["export_dir"] == str(tmp_path / "x")
    dialog._detect_export()
    assert dialog.values()["export_dir"] == "auto"


def _card(state=JobState.WAITING, **extra):
    status = gui.JobStatus(job_id="j1", url="https://www.twitch.tv/gaules", platform="twitch",
                           channel="gaules", state=JobState.WAITING, message="")
    return status, gui.JobCard(dataclasses.replace(status, state=state, **extra))


def test_job_card_send_menu(app):
    status, card = _card()
    assert card.export_button.text() == "Enviar ▾"
    assert not card.export_button.isVisibleTo(card)          # aguardando a live
    card.update_status(dataclasses.replace(status, state=JobState.RECORDING))
    assert card.export_button.isVisibleTo(card)
    assert not card.export_button.isEnabled()
    assert card.export_button.toolTip()
    card.update_status(dataclasses.replace(status, state=JobState.RECORDING, final_clips=(Path("a.mp4"),)))
    assert card.export_button.isEnabled()
    assert [a.text() for a in card.export_button.menu().actions()] == ["Google Drive", "YouTube"]
    drive, youtube = [], []
    card.export_requested.connect(drive.append)
    card.youtube_requested.connect(youtube.append)
    card.drive_action.trigger()
    assert drive == ["j1"] and youtube == []
    card.youtube_action.trigger()
    assert youtube == ["j1"]


def test_job_card_youtube_text(app):
    _status, card = _card(JobState.DONE)
    assert not card.youtube_label.isVisibleTo(card)
    card.set_youtube_text("YouTube: 3 postados")
    assert card.youtube_label.isVisibleTo(card) and card.youtube_label.text() == "YouTube: 3 postados"
    card.set_youtube_text("")
    assert not card.youtube_label.isVisibleTo(card)


def test_export_dialog_list_and_selection(app, tmp_path):
    clips = _make_clips(tmp_path / "sessao" / "final")
    dest = tmp_path / "drive" / "sessao"
    dest.mkdir(parents=True)
    (dest / clips[0].name).write_bytes(clips[0].read_bytes())
    dialog = gui.ExportDialog(clips, dest, "gaules")
    assert dialog.list.count() == 3
    first = dialog.list.item(0)
    assert first.checkState() == Qt.CheckState.Unchecked
    assert "já está no Drive" in first.text()
    assert dialog.list.item(1).checkState() == Qt.CheckState.Checked
    assert "já está" not in dialog.list.item(1).text()
    assert dialog.count_label.text().startswith("2 selecionados")
    assert dialog.send_button.text() == "Enviar 2 clipes"
    dialog.select_none_button.click()
    assert dialog.count_label.text().startswith("0 selecionados")
    assert not dialog.send_button.isEnabled()
    dialog.select_all_button.click()
    assert dialog.count_label.text().startswith("3 selecionados")
    assert dialog.send_button.isEnabled()


def test_export_dialog_full_send(app, tmp_path):
    clips = _make_clips(tmp_path / "sessao" / "final")
    dest = tmp_path / "drive" / "sessao"
    dialog = gui.ExportDialog(clips, dest, "gaules")
    dialog.show()
    dialog.send_button.click()
    assert dialog._busy
    assert _wait_until(lambda: not dialog._busy)
    dialog.wait_finished()
    for clip in clips:
        assert (dest / clip.name).read_bytes() == clip.read_bytes()
    assert not list(dest.glob("*.part"))
    assert "3 clipes copiados para o Drive" in dialog.status_label.text()
    assert dialog.open_folder_button.isVisibleTo(dialog)
    assert all(dialog.list.item(i).checkState() == Qt.CheckState.Unchecked for i in range(3))
    assert "já está no Drive" in dialog.list.item(0).text()
    dialog.close()


def test_export_dialog_error_is_friendly(app, tmp_path):
    clips = _make_clips(tmp_path / "sessao" / "final", 1)
    dialog = gui.ExportDialog(clips, tmp_path / "drive", "gaules")
    clips[0].unlink()
    dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.start_export()
    assert _wait_until(lambda: not dialog._busy)
    assert "Não deu para copiar" in dialog.status_label.text()
    dialog.close()


def test_export_clips_without_destination_warns(window, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "detect_drive_folder", lambda: None)
    window.url_edit.setText("https://www.twitch.tv/gaules")
    window.add_live()
    job_id = next(iter(window._cards))
    asked = []
    opened = []
    monkeypatch.setattr(window, "confirm", lambda title, text: asked.append((title, text)) or True)
    monkeypatch.setattr(window, "open_settings", lambda: opened.append(True))
    window._export_clips(job_id)
    assert asked and "Google Drive" in asked[0][1]
    assert opened == [True]


# ───────────────────────────── postar no YouTube (ADR-012) ──────────────────────
import uploader


class FakeYouTubeClient:
    def __init__(self, *, configured=True, connected=True, connect_error: str = "") -> None:
        self.configured = configured
        self.connected = connected
        self.connect_error = connect_error
        self.installed: list[Path] = []
        self.connect_calls = 0

    def is_configured(self) -> bool:
        return self.configured

    def is_connected(self) -> bool:
        return self.connected

    def install_client_secret(self, source: Path) -> None:
        self.installed.append(Path(source))
        self.configured = True

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error:
            raise uploader.AuthError(self.connect_error)
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False


class FakeQueue:
    def __init__(self, client=None, on_event=None) -> None:
        self.client = client
        self.on_event = on_event
        self.submitted: list[tuple[Path, uploader.VideoMeta, Path | None]] = []
        self.shutdown_calls = 0

    def submit(self, clip, meta, session_dir=None) -> bool:
        if uploader.is_posted(clip, session_dir) is not None:
            return False
        if any(c == clip for c, _m, _s in self.submitted):
            return False
        self.submitted.append((Path(clip), meta, session_dir))
        return True

    def pending(self) -> int:
        return len(self.submitted)

    def shutdown(self, wait=False, timeout_s=10.0) -> None:
        self.shutdown_calls += 1


@pytest.fixture
def yt_window(app, tmp_path):
    client = FakeYouTubeClient()
    win = gui.MainWindow(
        lambda cb: gui.FakeJobManager(cb), settings_path=tmp_path / "settings.json",
        hardware_probe=lambda: "CPU", check_ffmpeg=lambda: True,
        youtube_client_factory=lambda: client, upload_queue_factory=FakeQueue)
    win.show()
    win.fake_client = client
    yield win
    win.manager.shutdown()
    win._disk_timer.stop()


def _new_live(win) -> str:
    win.url_edit.setText("https://www.twitch.tv/gaules")
    win.add_live()
    return next(iter(win._cards))


def _enable_auto(win, value=True) -> None:
    values = dict(win._settings)
    values["youtube_auto_upload"] = value
    win.apply_new_settings(values)


def test_youtube_settings_defaults():
    defaults = gui.default_settings()
    assert defaults["youtube_privacy"] == config.YOUTUBE_PRIVACY
    assert defaults["youtube_auto_upload"] is False
    assert "youtube_privacy" in gui.SETTINGS_KEYS and "youtube_auto_upload" in gui.SETTINGS_KEYS


def test_settings_dialog_youtube_values(app):
    dialog = gui.SettingsDialog(gui.default_settings(), youtube_client=FakeYouTubeClient())
    assert dialog.values()["youtube_auto_upload"] is False
    dialog.youtube_privacy_combo.setCurrentIndex(dialog.youtube_privacy_combo.findData("private"))
    dialog.youtube_auto_check.setChecked(True)
    values = dialog.values()
    assert values["youtube_privacy"] == "private" and values["youtube_auto_upload"] is True
    assert [dialog.youtube_privacy_combo.itemText(i) for i in range(3)] == ["Público", "Não listado", "Privado"]


def test_settings_dialog_youtube_status(app):
    cases = [(dict(configured=False, connected=False), "Falta o arquivo de credencial"),
             (dict(configured=True, connected=False), "Não conectado"),
             (dict(configured=True, connected=True), "Conectado")]
    for kwargs, text in cases:
        dialog = gui.SettingsDialog(gui.default_settings(), youtube_client=FakeYouTubeClient(**kwargs))
        dialog.show()
        assert dialog.youtube_status_label.text() == text
        connected = kwargs["connected"]
        assert dialog.connect_button.isVisibleTo(dialog) is (not connected)
        assert dialog.disconnect_button.isVisibleTo(dialog) is connected
        dialog.close()


def test_settings_dialog_connect_flow_installs_secret_then_logs_in(app, tmp_path, monkeypatch):
    client = FakeYouTubeClient(configured=False, connected=False)
    picked = tmp_path / "credencial.json"
    asked = []
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileName",
                        lambda *a, **k: asked.append(a[1]) or (str(picked), ""))
    dialog = gui.SettingsDialog(gui.default_settings(), youtube_client=client)
    dialog.show()
    dialog.connect_button.click()
    assert client.installed == [picked]
    assert "credencial (.json)" in asked[0]
    assert _wait_until(lambda: dialog.youtube_status_label.text() == "Conectado")
    dialog.wait_login()
    assert client.connect_calls == 1
    assert dialog.disconnect_button.isVisibleTo(dialog)
    dialog.disconnect_button.click()
    assert dialog.youtube_status_label.text() == "Não conectado"
    assert dialog.connect_button.isVisibleTo(dialog)
    # já configurado: não pergunta o arquivo de novo
    dialog.connect_button.click()
    assert len(asked) == 1
    dialog.wait_login()
    dialog.close()


def test_settings_dialog_login_message_while_waiting(app, monkeypatch):
    gate = threading.Event()

    class SlowClient(FakeYouTubeClient):
        def connect(self):
            gate.wait(5)
            super().connect()

    dialog = gui.SettingsDialog(gui.default_settings(), youtube_client=SlowClient(connected=False))
    dialog.show()
    dialog.connect_button.click()
    assert "Abrimos o navegador" in dialog.youtube_message_label.text()
    assert not dialog.connect_button.isEnabled()
    gate.set()
    assert _wait_until(lambda: dialog.youtube_status_label.text() == "Conectado")
    dialog.wait_login()
    dialog.close()


def test_settings_dialog_connect_cancel_and_errors(app, tmp_path, monkeypatch):
    client = FakeYouTubeClient(configured=False, connected=False)
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    dialog = gui.SettingsDialog(gui.default_settings(), youtube_client=client)
    dialog.connect_button.click()
    assert client.installed == [] and client.connect_calls == 0

    def refuse(source):
        raise uploader.AuthError('Essa credencial é do tipo "Aplicativo da Web".')

    client.install_client_secret = refuse
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileName", lambda *a, **k: (str(tmp_path / "x.json"), ""))
    dialog.show()
    dialog.connect_button.click()
    assert "Aplicativo da Web" in dialog.youtube_message_label.text()
    assert client.connect_calls == 0

    failing = FakeYouTubeClient(configured=True, connected=False, connect_error="Não deu para concluir o login")
    second = gui.SettingsDialog(gui.default_settings(), youtube_client=failing)
    second.show()
    second.connect_button.click()
    assert _wait_until(lambda: "Não deu para concluir" in second.youtube_message_label.text())
    assert second.youtube_status_label.text() == "Não conectado"
    second.wait_login()
    assert second.connect_button.isEnabled()
    dialog.close()
    second.close()


def test_settings_dialog_help_opens_readme(app, monkeypatch):
    opened = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    dialog = gui.SettingsDialog(gui.default_settings(), youtube_client=FakeYouTubeClient())
    dialog.youtube_help_button.click()
    assert opened and (opened[0].endswith("README.md") or opened[0] == gui.README_URL)
    monkeypatch.setattr(gui.runtime, "app_dir", lambda: Path("pasta_que_nao_existe"))
    dialog.youtube_help_button.click()
    assert opened[1] == gui.README_URL and opened[1].endswith("#postar-no-youtube")


def _yt_dialog(tmp_path, queue=None, n=3, **kwargs):
    session = tmp_path / "sessao"
    clips = _make_clips(session / "final", n)
    dialog = gui.YouTubeDialog(clips, "gaules", "twitch", queue if queue is not None else FakeQueue(),
                               session_dir=session, **kwargs)
    return dialog, clips, session


def test_youtube_dialog_lists_clips_with_titles_and_posted_state(app, tmp_path):
    session = tmp_path / "sessao"
    clips = _make_clips(session / "final", 3)
    uploader.save_record(session, uploader.UploadRecord(
        clips[0].name, "v1", "https://youtube.com/shorts/v1", "public", 1.0))
    dialog = gui.YouTubeDialog(clips, "gaules", "twitch", FakeQueue(), session_dir=session)
    assert dialog.windowTitle() == "Postar clipes de gaules no YouTube"
    table = dialog.table
    assert table.rowCount() == 3 and table.columnCount() == 4
    assert [table.horizontalHeaderItem(c).text() for c in (1, 2)] == ["Clipe", "Título"]
    posted_check = table.item(0, 0)
    assert posted_check.checkState() == Qt.CheckState.Unchecked
    assert not posted_check.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert table.item(0, 2).text() == "postado · https://youtube.com/shorts/v1"
    assert not table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable
    assert "clip_01.mp4" in table.item(0, 1).text()
    assert table.item(1, 0).checkState() == Qt.CheckState.Checked
    assert table.item(1, 2).flags() & Qt.ItemFlag.ItemIsEditable
    assert table.item(1, 2).text() == "gaules | momento #2 da live #Shorts"
    assert dialog.count_label.text().startswith("2 selecionados")
    assert dialog.send_button.text() == "Postar 2 clipes"
    assert "continuam em segundo plano" in dialog.footer_label.text()
    dialog.select_none_button.click()
    assert dialog.send_button.text() == "Nenhum selecionado" and not dialog.send_button.isEnabled()
    dialog.select_all_button.click()
    assert dialog.selected_clips() == clips[1:]       # o já postado nunca é marcado


def test_youtube_dialog_double_click_opens_link_or_video(app, tmp_path, monkeypatch):
    session = tmp_path / "sessao"
    clips = _make_clips(session / "final", 2)
    uploader.save_record(session, uploader.UploadRecord(
        clips[0].name, "v1", "https://youtube.com/shorts/v1", "public", 1.0))
    dialog = gui.YouTubeDialog(clips, "gaules", "twitch", FakeQueue(), session_dir=session)
    opened = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    dialog._open_cell(0, 1)
    assert opened == ["https://youtube.com/shorts/v1"]
    dialog._open_cell(1, 1)
    assert opened[1].startswith("file:") and opened[1].endswith("clip_02.mp4")
    dialog._open_cell(1, 2)                        # coluna do título: edição, não abre nada
    assert len(opened) == 2


def test_youtube_dialog_submits_edited_titles_and_combo_privacy(app, tmp_path):
    queue = FakeQueue()
    dialog, clips, session = _yt_dialog(tmp_path, queue, privacy="public")
    assert dialog.privacy_combo.currentData() == "public"
    dialog.table.item(1, 2).setText("Meu título <editado>")
    dialog.table.item(2, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.privacy_combo.setCurrentIndex(dialog.privacy_combo.findData("unlisted"))
    assert dialog.send_button.text() == "Postar 2 clipes"
    dialog.send_button.click()
    assert [c for c, _m, _s in queue.submitted] == [clips[0], clips[1]]
    assert all(s == session for _c, _m, s in queue.submitted)
    assert queue.submitted[1][1].title == "Meu título editado"       # "<" e ">" removidos
    assert all(m.privacy == "unlisted" for _c, m, _s in queue.submitted)
    assert dialog.table.item(0, 3).text() == "na fila"
    assert dialog.table.item(2, 3).text() == ""
    assert dialog.selected_clips() == []                              # enviados saem da seleção
    assert not dialog.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable


def test_youtube_dialog_progress_done_failed_and_private_warning(app, tmp_path):
    queue = FakeQueue()
    dialog, clips, session = _yt_dialog(tmp_path, queue, n=2, privacy="public")
    dialog.send_button.click()
    ev = uploader.UploadEvent
    dialog.handle_event(ev("started", clips[0], total_bytes=1000))
    assert dialog.table.item(0, 3).text() == "enviando 0%"
    dialog.handle_event(ev("progress", clips[0], done_bytes=450, total_bytes=1000))
    assert dialog.table.item(0, 3).text() == "enviando 45%"
    record = uploader.UploadRecord(clips[0].name, "abc", "https://youtube.com/shorts/abc", "private", 1.0)
    dialog.handle_event(ev("done", clips[0], record=record))
    assert dialog.table.item(0, 3).text() == "postado"
    assert dialog.table.item(0, 2).text() == "postado · https://youtube.com/shorts/abc"
    assert dialog.warning_label.text() == ("O YouTube deixou o vídeo como privado. Projetos novos "
                                           "podem precisar de verificação do Google.")
    assert not dialog.warning_label.isHidden()
    dialog.handle_event(ev("failed", clips[1], error="rede caiu", error_kind="other"))
    assert dialog.table.item(1, 3).text() == "falhou: rede caiu"
    assert dialog.table.item(1, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable      # pode tentar de novo
    dialog.handle_event(ev("progress", tmp_path / "outro.mp4", done_bytes=1, total_bytes=2))   # ignorado


def test_youtube_dialog_no_warning_when_privacy_matches(app, tmp_path):
    dialog, clips, _session = _yt_dialog(tmp_path, n=1, privacy="private")
    dialog.send_button.click()
    record = uploader.UploadRecord(clips[0].name, "abc", "https://youtube.com/shorts/abc", "private", 1.0)
    dialog.handle_event(uploader.UploadEvent("done", clips[0], record=record))
    assert dialog.warning_label.isHidden()


def test_youtube_dialog_close_does_not_cancel_queue(app, tmp_path):
    queue = FakeQueue()
    dialog, _clips, _session = _yt_dialog(tmp_path, queue)
    dialog.show()
    dialog.send_button.click()
    dialog.close()
    assert queue.shutdown_calls == 0 and len(queue.submitted) == 3


def test_youtube_dialog_not_connected_offers_settings(app, tmp_path):
    dialog, _clips, _session = _yt_dialog(tmp_path, None, connected=False)
    dialog.show()
    assert dialog.not_connected_label.isVisibleTo(dialog)
    assert not dialog.table.isVisibleTo(dialog) and not dialog.send_button.isVisibleTo(dialog)
    asked = []
    dialog.settings_requested.connect(lambda: asked.append(True))
    dialog.settings_button.click()
    assert asked == [True]
    dialog.close()


def test_card_youtube_action_opens_dialog_with_shared_queue(yt_window, tmp_path, monkeypatch):
    job_id = _new_live(yt_window)
    clips = _make_clips(tmp_path / "sessao" / "final", 2)
    yt_window.manager.update(job_id, state=JobState.DONE, final_clips=tuple(clips),
                             session_dir=tmp_path / "sessao")
    shown = []
    monkeypatch.setattr(gui.YouTubeDialog, "exec", lambda self: shown.append(self) or 0)
    yt_window._cards[job_id].youtube_action.trigger()
    assert len(shown) == 1 and shown[0].table.rowCount() == 2
    assert shown[0]._queue is yt_window.upload_queue()
    assert yt_window._youtube_dialog is None                      # solto depois do exec
    yt_window.fake_client.connected = False
    yt_window._cards[job_id].youtube_action.trigger()
    assert not shown[1]._connected


def test_auto_post_only_new_clips_and_never_duplicates(yt_window, tmp_path):
    job_id = _new_live(yt_window)
    clips = _make_clips(tmp_path / "sessao" / "final", 4)
    session = tmp_path / "sessao"
    # Opção desligada: o clipe 1 aparece e é só "visto".
    yt_window.manager.update(job_id, state=JobState.RECORDING, final_clips=(clips[0],), session_dir=session)
    assert yt_window._yt_queue is None
    _enable_auto(yt_window, True)
    # Ligou a opção: o clipe antigo NÃO é postado retroativamente; o novo é.
    yt_window.manager.update(job_id, final_clips=(clips[0], clips[1]))
    queue = yt_window.upload_queue()
    assert [c for c, _m, _s in queue.submitted] == [clips[1]]
    # Mesmo status de novo: não duplica.
    yt_window.manager.update(job_id, message="gravando")
    yt_window.manager.update(job_id, final_clips=(clips[0], clips[1], clips[2]))
    assert [c for c, _m, _s in queue.submitted] == [clips[1], clips[2]]
    meta = queue.submitted[0][1]
    assert meta.title == "gaules | momento #2 da live #Shorts" and meta.privacy == config.YOUTUBE_PRIVACY
    assert queue.submitted[0][2] == session


def test_auto_post_respects_settings_connection_and_privacy(yt_window, tmp_path):
    job_id = _new_live(yt_window)
    clips = _make_clips(tmp_path / "sessao" / "final", 3)
    values = dict(yt_window._settings, youtube_auto_upload=True, youtube_privacy="unlisted")
    yt_window.apply_new_settings(values)
    yt_window.fake_client.connected = False
    yt_window.manager.update(job_id, state=JobState.RECORDING, final_clips=(clips[0],))
    assert yt_window._yt_queue is None                           # sem login: nada é enfileirado
    yt_window.fake_client.connected = True
    yt_window.manager.update(job_id, final_clips=(clips[0], clips[1]))
    queue = yt_window.upload_queue()
    assert [c for c, _m, _s in queue.submitted] == [clips[1]]    # o 1 foi "visto" sem login
    assert queue.submitted[0][1].privacy == "unlisted"
    _enable_auto(yt_window, False)
    yt_window.manager.update(job_id, final_clips=(clips[0], clips[1], clips[2]))
    assert len(queue.submitted) == 1


def test_auto_post_skips_clips_already_there_at_startup(app, tmp_path):
    client, queues = FakeYouTubeClient(), []
    settings = tmp_path / "settings.json"
    gui.save_settings(dict(gui.default_settings(), youtube_auto_upload=True), settings)
    clips = _make_clips(tmp_path / "sessao" / "final", 2)

    def factory(cb):
        manager = gui.FakeJobManager(cb)
        manager._create("https://www.twitch.tv/gaules", "twitch", "gaules", JobState.DONE,
                        "pronto", final_clips=tuple(clips), session_dir=tmp_path / "sessao")
        return manager

    win = gui.MainWindow(factory, settings_path=settings, hardware_probe=lambda: "CPU",
                         check_ffmpeg=lambda: True, youtube_client_factory=lambda: client,
                         upload_queue_factory=lambda c, cb: queues.append(FakeQueue(c, cb)) or queues[-1])
    assert queues == []
    win._disk_timer.stop()


def test_upload_events_drive_card_text(yt_window, tmp_path):
    job_id = _new_live(yt_window)
    clips = _make_clips(tmp_path / "sessao" / "final", 2)
    yt_window.manager.update(job_id, state=JobState.DONE, final_clips=tuple(clips))
    card = yt_window._cards[job_id]
    ev = uploader.UploadEvent
    yt_window._on_upload_event(ev("queued", clips[0]))
    yt_window._on_upload_event(ev("queued", clips[1]))
    assert card.youtube_label.text() == "YouTube: enviando 1 de 2"
    record = uploader.UploadRecord(clips[0].name, "a", "https://youtube.com/shorts/a", "public", 1.0)
    yt_window._on_upload_event(ev("done", clips[0], record=record))
    assert card.youtube_label.text() == "YouTube: enviando 2 de 2"
    yt_window._on_upload_event(ev("done", clips[1], record=record))
    assert card.youtube_label.text() == "YouTube: 2 postados"
    assert card.youtube_label.isVisibleTo(card)


def test_auth_failure_notifies_once_and_pauses_auto_post(yt_window, tmp_path):
    job_id = _new_live(yt_window)
    clips = _make_clips(tmp_path / "sessao" / "final", 3)
    _enable_auto(yt_window, True)
    yt_window.manager.update(job_id, state=JobState.RECORDING, final_clips=(clips[0],))
    queue = yt_window.upload_queue()
    ev = uploader.UploadEvent
    yt_window._on_upload_event(ev("failed", clips[0], error="Login do YouTube expirou", error_kind="auth"))
    assert not yt_window.notice_label.isHidden() and "login expirou" in yt_window.notice_label.text()
    yt_window.notice_label.setText("já li")
    yt_window._on_upload_event(ev("failed", clips[1], error="Login do YouTube expirou", error_kind="auth"))
    assert yt_window.notice_label.text() == "já li"                # não repete o aviso
    yt_window.manager.update(job_id, final_clips=(clips[0], clips[1]))
    assert len(queue.submitted) == 1                               # auto-post em pausa
    assert yt_window._auto_post_blocked


def test_quota_failure_notice_and_done_clears_it(yt_window, tmp_path):
    job_id = _new_live(yt_window)
    clips = _make_clips(tmp_path / "sessao" / "final", 1)
    yt_window.manager.update(job_id, final_clips=tuple(clips))
    ev = uploader.UploadEvent
    yt_window._on_upload_event(ev("failed", clips[0], error="Cota", error_kind="quota"))
    assert "cota diária" in yt_window.notice_label.text()
    assert not yt_window._auto_post_blocked                        # cota não desliga o auto-post
    record = uploader.UploadRecord(clips[0].name, "a", "https://youtube.com/shorts/a", "public", 1.0)
    yt_window._on_upload_event(ev("done", clips[0], record=record))
    assert yt_window.notice_label.isHidden()


def test_open_settings_reconnect_resumes_auto_post(yt_window, monkeypatch):
    yt_window._auto_post_blocked = True
    monkeypatch.setattr(gui.SettingsDialog, "exec", lambda self: 0)
    yt_window.fake_client.connected = False
    yt_window.open_settings()
    assert yt_window._auto_post_blocked
    yt_window.fake_client.connected = True
    yt_window.open_settings()
    assert not yt_window._auto_post_blocked


def test_close_shuts_down_upload_queue(yt_window, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    queue = yt_window.upload_queue()
    clip = Path("a/final/clip_01.mp4")
    queue.submit(clip, uploader.build_meta(clip, channel="c", platform="twitch"))
    asked = []
    monkeypatch.setattr(yt_window, "confirm", lambda title, text: asked.append(text) or True)
    event = QCloseEvent()
    yt_window.closeEvent(event)
    assert event.isAccepted() and queue.shutdown_calls == 1
    assert asked and "YouTube" in asked[0]


# ───────────────────────── Abrir gravação (lives antigas) ─────────────────────────
from PySide6.QtWidgets import QFileDialog


def _recorded(tmp_path: Path, name="20261118-091500_twitch_alanzoka", clips=3, manifest=True) -> Path:
    root = tmp_path / name
    final = root / "final"
    final.mkdir(parents=True)
    for i in range(clips):
        (final / f"clip_{i:02d}.mp4").write_bytes(b"x")
    if manifest:
        (root / "live_00.ts").write_bytes(b"x" * 10)
        (root / "session.json").write_text(json.dumps({
            "url": "https://www.twitch.tv/alanzoka", "platform": "twitch", "channel": "alanzoka",
            "created_at": 1.0,
            "segments": [{"index": 0, "path": "live_00.ts", "wall_start": 100.0, "wall_end": 160.0}],
        }), encoding="utf-8")
    return root


def test_load_recorded_session_from_session_and_final_dir(tmp_path):
    root = _recorded(tmp_path)
    (root / "final" / "x.mp4.part").write_bytes(b"x")
    status = gui.load_recorded_session(root)
    assert status.job_id == "disk:" + str(root.resolve())
    assert status.state is JobState.DONE and status.message == "Gravação aberta do disco"
    assert (status.channel, status.platform, status.url) == ("alanzoka", "twitch", "https://www.twitch.tv/alanzoka")
    assert [c.name for c in status.final_clips] == ["clip_00.mp4", "clip_01.mp4", "clip_02.mp4"]
    assert status.clips_done == status.clips_total == status.spikes == 3
    assert status.recorded_s == 60.0 and status.recorded_bytes == 10
    assert status.session_dir == root.resolve()
    assert gui.load_recorded_session(root / "final").job_id == status.job_id


def test_load_recorded_session_falls_back_to_folder_name(tmp_path):
    root = _recorded(tmp_path, name="20261118-091500_kick_foo_bar", manifest=False)
    status = gui.load_recorded_session(root)
    assert (status.platform, status.channel, status.url) == ("kick", "foo_bar", "")
    assert status.recorded_s == 0 and status.recorded_bytes == 0


def test_load_recorded_session_errors(tmp_path):
    empty = _recorded(tmp_path, clips=0)
    with pytest.raises(ValueError, match="ainda não tem clipes"):
        gui.load_recorded_session(empty)
    (empty / "final" / "a.mp4.part").write_bytes(b"x")
    with pytest.raises(ValueError, match="ainda não tem clipes"):
        gui.load_recorded_session(empty)
    other = tmp_path / "qualquer"
    other.mkdir()
    with pytest.raises(ValueError, match="não parece ser de uma live"):
        gui.load_recorded_session(other)


def _pick(monkeypatch, folder):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))


def test_open_recording_creates_card_once_and_reports_errors(yt_window, tmp_path, monkeypatch):
    root = _recorded(tmp_path)
    _pick(monkeypatch, root)
    yt_window.open_recording()
    assert list(yt_window._cards) == ["disk:" + str(root.resolve())]
    card = yt_window._cards[next(iter(yt_window._cards))]
    assert card.status.state is JobState.DONE and yt_window.active_count() == 0
    yt_window.open_recording()
    assert len(yt_window._cards) == 1
    _pick(monkeypatch, tmp_path / "qualquer")
    (tmp_path / "qualquer").mkdir()
    yt_window.open_recording()
    assert "não parece ser de uma live" in yt_window.error_label.text()
    _pick(monkeypatch, "")
    yt_window.open_recording()                                   # cancelar: nada muda
    assert len(yt_window._cards) == 1


def test_disk_card_never_auto_posts_and_remove_skips_manager(yt_window, tmp_path, monkeypatch):
    _enable_auto(yt_window, True)
    root = _recorded(tmp_path)
    _pick(monkeypatch, root)
    yt_window.open_recording()
    assert yt_window._yt_queue is None
    job_id = next(iter(yt_window._cards))
    calls = []
    monkeypatch.setattr(yt_window.manager, "remove", lambda j: calls.append(j))
    yt_window._remove_job(job_id)
    assert calls == [] and not yt_window._cards
