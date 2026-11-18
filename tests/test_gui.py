"""Testes da interface gráfica, em modo offscreen e com FakeJobManager."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
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
    saved = (config.OUTPUT_LAYOUT, config.WHISPER_LANGUAGE, config.SPIKE_MULTIPLIER, config.SESSIONS_DIR)
    yield
    (config.OUTPUT_LAYOUT, config.WHISPER_LANGUAGE, config.SPIKE_MULTIPLIER, config.SESSIONS_DIR) = saved


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
              "spike_multiplier": 2.2, "sessions_dir": str(tmp_path / "saida")}
    gui.save_settings(values, path)
    assert json.loads(path.read_text(encoding="utf-8"))["output_layout"] == "horizontal"
    loaded = gui.load_settings(path)
    assert loaded == values
    gui.apply_settings(loaded)
    assert config.OUTPUT_LAYOUT == "horizontal"
    assert config.WHISPER_LANGUAGE is None
    assert config.SPIKE_MULTIPLIER == 2.2
    assert config.SESSIONS_DIR == str(tmp_path / "saida")


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
