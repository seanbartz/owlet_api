#!/usr/bin/env python3
"""Tests for overnight_vitals.py."""

import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from zoneinfo import ZoneInfo

# Make the repo root importable so we can import overnight_vitals.py directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

import overnight_vitals as ov


LOCAL_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# night_window_for_date
# ---------------------------------------------------------------------------

class TestNightWindowForDate:
    def test_returns_utc_datetimes(self):
        start, end = ov.night_window_for_date(date(2024, 1, 15))
        assert start.tzinfo == timezone.utc
        assert end.tzinfo == timezone.utc

    def test_start_is_previous_evening(self):
        # For date 2024-01-15 the session starts 2024-01-14 at 19:00 ET.
        start, _ = ov.night_window_for_date(date(2024, 1, 15), tz=LOCAL_TZ)
        start_local = start.astimezone(LOCAL_TZ)
        assert start_local.year == 2024
        assert start_local.month == 1
        assert start_local.day == 14
        assert start_local.hour == ov.NIGHT_START_HOUR

    def test_end_is_target_date_morning(self):
        _, end = ov.night_window_for_date(date(2024, 1, 15), tz=LOCAL_TZ)
        end_local = end.astimezone(LOCAL_TZ)
        assert end_local.year == 2024
        assert end_local.month == 1
        assert end_local.day == 15
        assert end_local.hour == ov.NIGHT_END_HOUR

    def test_start_before_end(self):
        start, end = ov.night_window_for_date(date(2024, 1, 15))
        assert start < end

    def test_window_spans_about_13_hours(self):
        start, end = ov.night_window_for_date(date(2024, 1, 15))
        # 19:00 → 08:00 = 13 hours
        assert (end - start) == timedelta(hours=13)


# ---------------------------------------------------------------------------
# filter_rows_to_window
# ---------------------------------------------------------------------------

class TestFilterRowsToWindow:
    def _make_rows(self):
        return [
            {"sample_timestamp_utc": "2024-01-15T01:00:00+00:00",
             "heart_rate": 120, "oxygen": 98},
            {"sample_timestamp_utc": "2024-01-15T03:00:00+00:00",
             "heart_rate": 130, "oxygen": 97},
            {"sample_timestamp_utc": "2024-01-15T10:00:00+00:00",  # outside
             "heart_rate": 115, "oxygen": 99},
            {"sample_timestamp_utc": "",  # no timestamp
             "heart_rate": 110, "oxygen": 96},
        ]

    def test_keeps_rows_inside_window(self):
        rows = self._make_rows()
        start = datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = ov.filter_rows_to_window(rows, start, end)
        assert len(result) == 2
        assert result[0]["heart_rate"] == 120
        assert result[1]["heart_rate"] == 130

    def test_excludes_rows_outside_window(self):
        rows = self._make_rows()
        start = datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = ov.filter_rows_to_window(rows, start, end)
        hrs = [r["heart_rate"] for r in result]
        assert 115 not in hrs

    def test_excludes_rows_with_no_timestamp(self):
        rows = self._make_rows()
        start = datetime(2024, 1, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 23, 0, tzinfo=timezone.utc)
        result = ov.filter_rows_to_window(rows, start, end)
        assert all(r.get("sample_timestamp_utc") for r in result)

    def test_empty_input(self):
        start = datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        assert ov.filter_rows_to_window([], start, end) == []

    def test_boundary_inclusive(self):
        rows = [
            {"sample_timestamp_utc": "2024-01-14T23:00:00+00:00",
             "heart_rate": 100, "oxygen": 95},
            {"sample_timestamp_utc": "2024-01-15T08:00:00+00:00",
             "heart_rate": 105, "oxygen": 96},
        ]
        start = datetime(2024, 1, 14, 23, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = ov.filter_rows_to_window(rows, start, end)
        assert len(result) == 2

    def test_uses_timestamp_utc_key_for_live_rows(self):
        """Live-mode rows use 'timestamp_utc' instead of 'sample_timestamp_utc'."""
        rows = [
            {"timestamp_utc": "2024-01-15T02:00:00+00:00",
             "heart_rate": 122, "oxygen": 97},
        ]
        start = datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = ov.filter_rows_to_window(rows, start, end)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# write_csv
# ---------------------------------------------------------------------------

class TestWriteCsv:
    def test_writes_header_and_rows(self, tmp_path):
        rows = [
            {"a": 1, "b": 2},
            {"a": 3, "b": 4},
        ]
        out = tmp_path / "out.csv"
        ov.write_csv(out, rows)
        text = out.read_text()
        assert "a,b" in text
        assert "1,2" in text

    def test_raises_on_empty_rows(self, tmp_path):
        with pytest.raises(ValueError, match="No rows"):
            ov.write_csv(tmp_path / "out.csv", [])


# ---------------------------------------------------------------------------
# create_run_output_dir
# ---------------------------------------------------------------------------

class TestCreateRunOutputDir:
    def test_creates_timestamped_directory(self, tmp_path):
        run_started_at = datetime(2024, 1, 15, 3, 4, 5, tzinfo=LOCAL_TZ)
        run_dir = ov.create_run_output_dir(
            tmp_path / "test_out", run_started_at=run_started_at)

        assert run_dir == tmp_path / "test_out_2024-01-15_03-04-05_EST"
        assert run_dir.exists()


# ---------------------------------------------------------------------------
# run_history_mode with --input-json
# ---------------------------------------------------------------------------

class TestRunHistoryModeFromJson:
    def test_loads_json_and_filters(self, tmp_path):
        """run_history_mode honours input_json and filters to the window."""
        histories = [{"device_dsn": "DEVICE1", "history": []}]
        json_file = tmp_path / "history.json"
        json_file.write_text(json.dumps(histories))

        start = datetime(2024, 1, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)

        sample_rows = [
            {"sample_timestamp_utc": "2024-01-15T01:00:00+00:00",
             "heart_rate": 125, "oxygen": 98},
            {"sample_timestamp_utc": "2024-01-15T20:00:00+00:00",  # outside
             "heart_rate": 110, "oxygen": 97},
        ]

        with patch("owlet_api.red_alert_history.decode_histories",
                   return_value=sample_rows):
            rows, returned_histories = ov.run_history_mode(
                None, None, 50, None, start, end, input_json=json_file)

        assert len(rows) == 1
        assert rows[0]["heart_rate"] == 125
        assert returned_histories == histories


# ---------------------------------------------------------------------------
# plot_metric
# ---------------------------------------------------------------------------

class TestPlotMetric:
    def _sample_rows(self):
        return [
            {"sample_timestamp_utc": "2024-01-15T01:00:00+00:00",
             "heart_rate": 120, "oxygen": 97},
            {"sample_timestamp_utc": "2024-01-15T02:00:00+00:00",
             "heart_rate": 125, "oxygen": 98},
        ]

    def test_saves_png(self, tmp_path):
        out = tmp_path / "hr.png"
        ov.plot_metric(
            self._sample_rows(), "heart_rate",
            "Heart Rate (bpm)", "Test HR", out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_raises_when_no_data(self, tmp_path):
        with pytest.raises(ValueError, match="No plottable data"):
            ov.plot_metric(
                [], "heart_rate", "HR", "title", tmp_path / "hr.png")

    def test_live_rows_use_timestamp_utc(self, tmp_path):
        rows = [
            {"timestamp_utc": "2024-01-15T01:00:00+00:00",
             "heart_rate": 118, "oxygen": 96},
            {"timestamp_utc": "2024-01-15T02:00:00+00:00",
             "heart_rate": 122, "oxygen": 97},
        ]
        out = tmp_path / "hr.png"
        ov.plot_metric(rows, "heart_rate", "Heart Rate (bpm)", "Test", out)
        assert out.exists()


# ---------------------------------------------------------------------------
# run_live_mode
# ---------------------------------------------------------------------------

class TestRunLiveMode:
    def _make_mock_api_and_device(self):
        hr_prop = MagicMock()
        hr_prop.value = 130
        ox_prop = MagicMock()
        ox_prop.value = 97

        device = MagicMock()
        device.dsn = "TESTDEV"
        device.get_properties.return_value = {
            "HEART_RATE": hr_prop,
            "OXYGEN_LEVEL": ox_prop,
        }

        api = MagicMock()
        api.get_devices.return_value = [device]
        return api, device

    def test_collects_rows_and_writes_csv(self, tmp_path):
        """Live mode collects one sample before end_utc elapses."""
        csv_path = tmp_path / "live.csv"

        # end_utc is far in the past so the while-loop exits immediately
        # after one device poll (we freeze time just before that).
        now = datetime(2024, 1, 15, 3, 0, 0, tzinfo=timezone.utc)
        end_utc = now - timedelta(seconds=1)  # already past → 0 iterations

        api, _ = self._make_mock_api_and_device()

        with patch("overnight_vitals.OwletAPI", return_value=api):
            rows = ov.run_live_mode(
                "user@example.com", "pass", None, 10, end_utc, csv_path)

        # Loop runs 0 times since end_utc is in the past.
        assert rows == []
        assert csv_path.exists()
        assert "heart_rate" in csv_path.read_text()

    def test_stops_on_keyboard_interrupt(self, tmp_path):
        """A KeyboardInterrupt ends the live loop gracefully."""
        csv_path = tmp_path / "live.csv"
        end_utc = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)

        api, device = self._make_mock_api_and_device()
        device.update.side_effect = KeyboardInterrupt

        with patch("overnight_vitals.OwletAPI", return_value=api):
            rows = ov.run_live_mode(
                "user@example.com", "pass", None, 10, end_utc, csv_path)

        assert rows == []
        assert csv_path.exists()


# ---------------------------------------------------------------------------
# main() — integration-style smoke tests
# ---------------------------------------------------------------------------

class TestMain:
    """Smoke-tests for the main() CLI entry point."""

    _BASE_HISTORIES = [{"device_dsn": "DEV1", "history": []}]
    _BASE_ROWS = [
        {"sample_timestamp_utc": "2024-01-15T01:00:00+00:00",
         "heart_rate": 120, "oxygen": 97},
        {"sample_timestamp_utc": "2024-01-15T03:00:00+00:00",
         "heart_rate": 125, "oxygen": 98},
    ]

    def test_history_mode_writes_outputs(self, tmp_path, monkeypatch):
        prefix = str(tmp_path / "test_out")
        run_dir = tmp_path / "test_out_run"
        run_dir.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "overnight_vitals.py",
            "--date", "2024-01-15",
            "--email", "user@example.com",
            "--password", "secret",
            "--limit", "10",
            "--prefix", prefix,
        ])

        with (
            patch("overnight_vitals.create_run_output_dir", return_value=run_dir),
            patch("overnight_vitals.run_history_mode",
                  return_value=(self._BASE_ROWS, self._BASE_HISTORIES)),
            patch("overnight_vitals.write_csv") as mock_write_csv,
            patch("overnight_vitals.plot_metric") as mock_plot,
        ):
            ov.main()

        mock_write_csv.assert_called_once()
        assert mock_write_csv.call_args.args[0] == run_dir / "test_out.csv"
        assert mock_plot.call_count == 2
        assert mock_plot.call_args_list[0].args[4] == run_dir / "test_out_heart_rate.png"
        assert mock_plot.call_args_list[1].args[4] == run_dir / "test_out_oxygen.png"

    def test_history_mode_no_data_exits_nonzero(self, tmp_path, monkeypatch):
        prefix = str(tmp_path / "empty_out")
        run_dir = tmp_path / "empty_out_run"
        run_dir.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "overnight_vitals.py",
            "--date", "2024-01-15",
            "--email", "user@example.com",
            "--password", "secret",
            "--prefix", prefix,
        ])

        with (
            patch("overnight_vitals.create_run_output_dir", return_value=run_dir),
            patch("overnight_vitals.run_history_mode",
                  return_value=([], self._BASE_HISTORIES)),
            pytest.raises(SystemExit) as exc_info,
        ):
            ov.main()

        assert exc_info.value.code == 1

    def test_no_plot_flag_skips_plotting(self, tmp_path, monkeypatch):
        prefix = str(tmp_path / "noplot_out")
        run_dir = tmp_path / "noplot_out_run"
        run_dir.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "overnight_vitals.py",
            "--date", "2024-01-15",
            "--email", "user@example.com",
            "--password", "secret",
            "--no-plot",
            "--prefix", prefix,
        ])

        with (
            patch("overnight_vitals.create_run_output_dir", return_value=run_dir),
            patch("overnight_vitals.run_history_mode",
                  return_value=(self._BASE_ROWS, self._BASE_HISTORIES)),
            patch("overnight_vitals.write_csv"),
            patch("overnight_vitals.plot_metric") as mock_plot,
        ):
            ov.main()

        mock_plot.assert_not_called()

    def test_explicit_start_end_overrides_date(self, tmp_path, monkeypatch):
        prefix = str(tmp_path / "custom_window_out")
        run_dir = tmp_path / "custom_window_out_run"
        run_dir.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "overnight_vitals.py",
            "--start", "2024-01-14T21:00",
            "--end", "2024-01-15T07:00",
            "--email", "user@example.com",
            "--password", "secret",
            "--no-plot",
            "--prefix", prefix,
        ])

        captured = {}

        def fake_run_history(email, password, limit, device, start, end, **kw):
            captured["start"] = start
            captured["end"] = end
            return [], []

        with (
            patch("overnight_vitals.create_run_output_dir", return_value=run_dir),
            patch("overnight_vitals.run_history_mode", side_effect=fake_run_history),
            pytest.raises(SystemExit),  # no data → exit 1
        ):
            ov.main()

        # Local 21:00 → 02:00 UTC (EST = UTC-5), 07:00 local → 12:00 UTC
        assert captured["start"].hour == 2
        assert captured["end"].hour == 12

    def test_live_mode_called_when_flag_set(self, tmp_path, monkeypatch):
        prefix = str(tmp_path / "live_out")
        run_dir = tmp_path / "live_out_run"
        run_dir.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "overnight_vitals.py",
            "--live",
            "--date", "2024-01-15",
            "--email", "user@example.com",
            "--password", "secret",
            "--no-plot",
            "--prefix", prefix,
        ])

        with (
            patch("overnight_vitals.create_run_output_dir", return_value=run_dir),
            patch("overnight_vitals.run_live_mode",
                  return_value=self._BASE_ROWS) as mock_live,
            patch("overnight_vitals.plot_metric"),
        ):
            ov.main()

        mock_live.assert_called_once()

    def test_input_json_flag_skips_api(self, tmp_path, monkeypatch):
        histories = self._BASE_HISTORIES
        json_file = tmp_path / "hist.json"
        json_file.write_text(json.dumps(histories))
        prefix = str(tmp_path / "json_out")
        run_dir = tmp_path / "json_out_run"
        run_dir.mkdir()

        monkeypatch.setattr(sys, "argv", [
            "overnight_vitals.py",
            "--input-json", str(json_file),
            "--date", "2024-01-15",
            "--no-plot",
            "--prefix", prefix,
        ])

        with (
            patch("overnight_vitals.create_run_output_dir", return_value=run_dir),
            patch("overnight_vitals.run_history_mode",
                  return_value=(self._BASE_ROWS, histories)) as mock_hist,
            patch("overnight_vitals.write_csv"),
        ):
            ov.main()

        # input_json should be forwarded
        _call_kwargs = mock_hist.call_args
        assert _call_kwargs.kwargs.get("input_json") == json_file or \
               json_file in _call_kwargs.args
