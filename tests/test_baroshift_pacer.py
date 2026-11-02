# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "numpy", "pandas", "scipy", "matplotlib", "straplog[plot]>=0.3"]
# ///
"""baroshift window_source = pacer, on synthetic StrapLog v3 exports.

    uv run tests/test_baroshift_pacer.py          # or: uv run --with pytest pytest tests

Everything is generated here (no real exports). The schema mirrors straplog's
own test fixtures (`make_v3`): v2 tables plus the v3 columns.
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).parents[1] / "skills/baroshift/scripts/baroshift.py"
spec = importlib.util.spec_from_file_location("baroshift", SCRIPT)
baroshift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baroshift)

TZ = -25200
START_MS = 1_792_097_374_000

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
CREATE TABLE recording(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  started_at_ms INTEGER NOT NULL, ended_at_ms INTEGER, tz_offset_s INTEGER NOT NULL,
  device_id TEXT, firmware TEXT, battery_start INTEGER, battery_end INTEGER, notes TEXT,
  activity TEXT NOT NULL DEFAULT 'other', context TEXT);
CREATE TABLE notification(id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL,
  received_at_ms INTEGER NOT NULL, hr_bpm INTEGER NOT NULL, contact INTEGER, n_rr INTEGER NOT NULL);
CREATE TABLE beat(id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL,
  notification_id INTEGER NOT NULL, idx INTEGER NOT NULL, rr_1024 INTEGER NOT NULL,
  rr_ms REAL NOT NULL, t_end_ms_est INTEGER NOT NULL, UNIQUE(notification_id, idx));
CREATE TABLE event(id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL,
  at_ms INTEGER NOT NULL, kind TEXT NOT NULL, note TEXT, label TEXT);
INSERT INTO meta VALUES ('format', 'straplog'), ('schema_version', '3'),
  ('app_version', '1.2 (9)'), ('event_kinds', '["start","stop","marker"]');
"""


def make_export(path, paced, total_s=1500, events=(), name="breathing test"):
    """One beat per notification. `paced` = [(from_s, to_s, rate_per_min)]:
    RSA at that rate (+-60 ms); elsewhere only faint 0.3 Hz noise."""
    rng = np.random.default_rng(1)
    con = sqlite3.connect(path)
    con.executescript(DDL)
    end_ms = START_MS + total_s * 1000
    con.execute("INSERT INTO recording(id, name, started_at_ms, ended_at_ms, tz_offset_s, activity) "
                "VALUES (1, ?, ?, ?, ?, 'breathing')", (name, START_MS, end_ms, TZ))
    t, n = 0.5, 0
    while t < total_s:
        rr = 1000.0 + rng.normal(0, 4)
        for a, b, rate in paced:
            if a <= t < b:
                rr += 60 * np.sin(2 * np.pi * rate / 60 * t)
        if not any(a <= t < b for a, b, _ in paced):
            rr += 8 * np.sin(2 * np.pi * 0.3 * t)
        n += 1
        rx = START_MS + round(t * 1000)
        con.execute("INSERT INTO notification VALUES (?, 1, ?, 60, 1, 1)", (n, rx))
        con.execute("INSERT INTO beat(recording_id, notification_id, idx, rr_1024, rr_ms, t_end_ms_est) "
                    "VALUES (1, ?, 0, ?, ?, ?)", (n, round(rr * 1.024), rr, rx))
        t += rr / 1000
    con.executemany("INSERT INTO event(recording_id, at_ms, kind, note, label) VALUES (1, ?, ?, ?, ?)",
                    [(START_MS + round(s * 1000), k, note, label) for s, k, note, label in events])
    con.commit()
    con.close()
    return path


def analyze(db, *extra):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert baroshift.main(["analyze", "--db", str(db), *extra]) == 0
    return json.loads(buf.getvalue())


def test_pacer_window_and_stamped_rate(tmp_path):
    # Paced at 5.0/min, which is inside the default band but not its centre:
    # the stamped rate must be reported and the band centred on it.
    db = make_export(tmp_path / "a.sqlite", [(300, 900, 5.0)], events=[
        (300, "pacer_start", "5.0/min in5.0 out7.0 t0=%d" % (START_MS + 300_000), None),
        (900, "pacer_stop", None, None)])
    out = analyze(db)
    (s,) = out["sessions"]
    assert s["window_source"] == "pacer"
    assert s["pacer"] == {"stamped_rate_bpm": 5.0, "inhale_s": 5.0, "exhale_s": 7.0, "stopped": True}
    assert s["pace_band_bpm"] == [3.5, 6.5]
    assert s["duration_min"] == pytest.approx(10.0, abs=0.1)
    assert s["breath_rate_bpm"] == pytest.approx(5.0, abs=0.5)
    assert s["peak_share"] >= 0.5
    assert s["start"] == "13:54:34"      # START 13:49:34 local + 300 s


def test_unstopped_pacer_ends_at_recording_end(tmp_path):
    db = make_export(tmp_path / "b.sqlite", [(900, 1500, 6.0)], events=[
        (900, "pacer_start", "6.0/min in5.0 out5.0", None)])
    (s,) = analyze(db)["sessions"]
    assert s["window_source"] == "pacer" and s["pacer"]["stopped"] is False
    assert 9.5 <= s["duration_min"] <= 10.1


def test_legacy_note(tmp_path):
    db = make_export(tmp_path / "c.sqlite", [(300, 900, 6.0)], events=[
        (300, "pacer_start", "6.0/min", None), (900, "pacer_stop", None, None)])
    (s,) = analyze(db)["sessions"]
    assert s["window_source"] == "pacer"
    assert s["pacer"]["inhale_s"] == s["pacer"]["exhale_s"] == 5.0
    assert s["breath_rate_bpm"] == pytest.approx(6.0, abs=0.5)


def test_pacer_beats_markers(tmp_path):
    db = make_export(tmp_path / "d.sqlite", [(300, 900, 6.0)], events=[
        (200, "marker", None, "start"), (1000, "marker", None, "end"),
        (300, "pacer_start", "6.0/min", None), (900, "pacer_stop", None, None)])
    (s,) = analyze(db)["sessions"]
    assert s["window_source"] == "pacer" and s["duration_min"] == pytest.approx(10.0, abs=0.1)


def test_manual_window_still_wins(tmp_path):
    db = make_export(tmp_path / "e.sqlite", [(300, 900, 6.0)], events=[
        (300, "pacer_start", "6.0/min", None), (900, "pacer_stop", None, None)])
    out = analyze(db, "--window", "13:55-14:00")
    assert [s["window_source"] for s in out["sessions"]] == ["manual"]


def test_no_pacer_falls_through_to_markers(tmp_path):
    db = make_export(tmp_path / "f.sqlite", [(300, 900, 6.0)], events=[
        (300, "marker", None, "start"), (900, "marker", None, "end")])
    (s,) = analyze(db)["sessions"]
    assert s["window_source"] == "markers" and "pacer" not in s


def test_no_pacer_no_markers_falls_through_to_spectral(tmp_path):
    db = make_export(tmp_path / "g.sqlite", [(300, 900, 6.0)])
    (s,) = analyze(db)["sessions"]
    assert s["window_source"] == "detected" and "pacer" not in s


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", *sys.argv[1:]]))
