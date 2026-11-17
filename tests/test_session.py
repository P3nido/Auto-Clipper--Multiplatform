import json

from session import RecordingSegment, Session


def test_create_structure_and_name(tmp_path):
    s = Session.create("https://www.twitch.tv/Some.Chan!", tmp_path)
    assert s.root.parent == tmp_path
    stamp, platform, channel = s.root.name.split("_", 2)
    assert len(stamp) == 15 and stamp[8] == "-"
    assert platform == "twitch"
    assert channel == "Some_Chan_"
    for d in (s.clips_dir, s.subs_dir, s.final_dir):
        assert d.is_dir()
    assert s.manifest_path.is_file()


def test_channel_sanitized_and_truncated(tmp_path):
    s = Session.create("https://kick.com/" + "a" * 100, tmp_path)
    assert s.root.name.endswith("_kick_" + "a" * 40)
    assert s.channel == "a" * 100


def test_new_segment_numbering(tmp_path):
    s = Session.create("https://kick.com/xqc", tmp_path)
    a, b = s.new_segment(), s.new_segment()
    assert (a.index, b.index) == (0, 1)
    assert a.path == s.root / "live_00.ts"
    assert b.path == s.root / "live_01.ts"
    data = json.loads(s.manifest_path.read_text(encoding="utf-8"))
    assert len(data["segments"]) == 2


def test_roundtrip(tmp_path):
    s = Session.create("https://www.twitch.tv/gaules", tmp_path)
    seg = s.new_segment()
    seg.wall_start, seg.wall_end = 100.0, 200.0
    seg.ad_breaks.append((120.0, 30.0))
    s.new_segment()
    s.status["recorder"] = "ok"
    s.save()

    data = json.loads(s.manifest_path.read_text(encoding="utf-8"))
    assert data["segments"][0]["path"] == "live_00.ts"  # relativo
    assert data["segments"][0]["ad_breaks"] == [[120.0, 30.0]]
    assert not (s.root / "session.json.tmp").exists()

    loaded = Session.load(s.root)
    assert loaded.url == s.url and loaded.platform == "twitch" and loaded.channel == "gaules"
    assert loaded.created_at == s.created_at
    assert loaded.status == {"recorder": "ok"}
    assert loaded.segments == s.segments
    assert loaded.segments[0].ad_breaks == [(120.0, 30.0)]
    assert loaded.segments[1].wall_start is None


def test_roundtrip_after_move(tmp_path):
    s = Session.create("https://kick.com/xqc", tmp_path / "a")
    s.new_segment()
    new_root = tmp_path / "moved"
    s.root.rename(new_root)
    loaded = Session.load(new_root)
    assert loaded.segments[0].path == new_root / "live_00.ts"


def test_segment_for(tmp_path):
    s = Session.create("https://kick.com/xqc", tmp_path)
    a, b, c = s.new_segment(), s.new_segment(), s.new_segment()
    a.wall_start, a.wall_end = 100.0, 200.0
    b.wall_start, b.wall_end = 300.0, 400.0
    c.wall_start, c.wall_end = 500.0, None  # aberto
    assert s.segment_for(150.0) is a
    assert s.segment_for(100.0) is a and s.segment_for(200.0) is a
    assert s.segment_for(250.0) is None
    assert s.segment_for(350.0) is b
    assert s.segment_for(99.0) is None
    assert s.segment_for(10_000.0) is c


def test_segment_for_ignores_unstarted(tmp_path):
    s = Session.create("https://kick.com/xqc", tmp_path)
    s.segments.append(RecordingSegment(index=0, path=s.root / "live_00.ts"))
    assert s.segment_for(1.0) is None
