#!/usr/bin/env python3
"""Download and visualize a whole night's Owlet heart rate and oxygen data.

Two modes:

  history (default)
      Fetch RED_ALERT_SUMMARY history from the Ayla cloud, decode it into
      per-sample heart-rate and oxygen readings, filter to the requested
      night window, write a CSV, and save PNG plots.

  live (--live)
      Poll HEART_RATE and OXYGEN_LEVEL every INTERVAL seconds and write
      samples to a CSV in real time so data is not lost on interruption.
      Plots are generated when the run finishes.  Each run gets its own
      timestamped output folder containing the CSV, JSON, and plots.

      Recording ends at the night-window end time by default, or immediately
      when you press Ctrl+C.  Use --duration to record for a fixed number of
      hours (fractions allowed) starting from now — this lets you record at
      any time of day, not just overnight.

Night window defaults to the previous evening (7 pm local) through the
current morning (8 am local).  Use --date, or --start/--end to override.

Usage examples
--------------
Download last night's data and plot it::

    python3 overnight_vitals.py

Download a specific night (night ending on 2024-01-15)::

    python3 overnight_vitals.py --date 2024-01-15

Record live for 2.5 hours starting now::

    python3 overnight_vitals.py --live --duration 2.5

Run live all night from 9 pm to 7 am, polling every 10 seconds::

    python3 overnight_vitals.py --live --start 2024-01-14T21:00 --end 2024-01-15T07:00

Re-use a previously downloaded JSON file (skips API calls)::

    python3 overnight_vitals.py --input-json my_history.json --date 2024-01-15
"""

import argparse
import csv
import getpass
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from owlet_api.owletapi import OwletAPI
from owlet_api.owletexceptions import OwletTemporaryCommunicationException


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

LOCAL_TIMEZONE = ZoneInfo("America/New_York")
NIGHT_START_HOUR = 19   # 7 pm  — default overnight session start
NIGHT_END_HOUR = 8      # 8 am  — default overnight session end


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def prompt_if_missing(value, prompt_text, secret=False):
    """Return *value* if truthy, otherwise prompt the user for it."""
    if value:
        return value
    if secret:
        return getpass.getpass(prompt_text)
    return input(prompt_text)


def night_window_for_date(target_date, tz=LOCAL_TIMEZONE):
    """Return (start_utc, end_utc) for the overnight session ending on *target_date* morning.

    The session begins at NIGHT_START_HOUR the evening *before* target_date
    and ends at NIGHT_END_HOUR on target_date itself.
    """
    start_local = datetime(
        target_date.year, target_date.month, target_date.day,
        NIGHT_START_HOUR, 0, 0, tzinfo=tz,
    ) - timedelta(days=1)
    end_local = datetime(
        target_date.year, target_date.month, target_date.day,
        NIGHT_END_HOUR, 0, 0, tzinfo=tz,
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def filter_rows_to_window(rows, start_utc, end_utc):
    """Return only the rows whose sample timestamp falls within [start_utc, end_utc]."""
    filtered = []
    for row in rows:
        ts_str = row.get("sample_timestamp_utc") or row.get("timestamp_utc", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if start_utc <= ts <= end_utc:
            filtered.append(row)
    return filtered


def create_run_output_dir(prefix, run_started_at=None, tz=LOCAL_TIMEZONE):
    """Create and return a timestamped output directory for one run."""
    base = Path(prefix)
    run_started_at = run_started_at or datetime.now(timezone.utc)
    stamp = run_started_at.astimezone(tz).strftime("%Y-%m-%d_%H-%M-%S_%Z")
    run_dir = base.parent / f"{base.name}_{stamp}"
    suffix = 2
    while run_dir.exists():
        run_dir = base.parent / f"{base.name}_{stamp}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


# ---------------------------------------------------------------------------
# History mode
# ---------------------------------------------------------------------------

def _load_histories_from_json(path):
    """Load previously saved history JSON (list-of-devices or bare list)."""
    value = json.loads(path.read_text())
    if (
        isinstance(value, list)
        and all(isinstance(item, dict) and "history" in item for item in value)
    ):
        return value
    return [{"device_dsn": "", "history": value}]


def run_history_mode(email, password, limit, device_dsn,
                     start_utc, end_utc, input_json=None):
    """Download (or load) RED_ALERT_SUMMARY history, decode, and filter to window.

    Parameters
    ----------
    email, password : str
        Owlet credentials (unused when *input_json* is provided).
    limit : int
        Maximum number of datapoints to request from the API.
    device_dsn : str or None
        If set, only the device with this DSN is queried.
    start_utc, end_utc : datetime
        Night window boundaries (timezone-aware UTC).
    input_json : Path or None
        If given, load history from this file instead of calling the API.

    Returns
    -------
    tuple[list[dict], list[dict]]
        *(rows, histories)* — decoded sample rows (filtered to the window)
        and the raw history list (suitable for saving as JSON).
    """
    if input_json is not None:
        histories = _load_histories_from_json(input_json)
    else:
        from owlet_api.red_alert_history import download_history
        histories = download_history(email, password, limit, device_dsn or None)

    from owlet_api.red_alert_history import decode_histories
    all_rows = decode_histories(histories)
    rows = filter_rows_to_window(all_rows, start_utc, end_utc)
    return rows, histories


# ---------------------------------------------------------------------------
# Live mode
# ---------------------------------------------------------------------------

def run_live_mode(email, password, device_dsn, interval, end_utc, csv_path):
    """Poll HEART_RATE and OXYGEN_LEVEL every *interval* seconds until *end_utc*.

    If *end_utc* is ``None``, recording continues indefinitely until the user
    presses Ctrl+C.  Samples are written to *csv_path* in real time.  Returns
    the collected rows as a list of dicts.
    """
    api = OwletAPI()
    api.set_email(email)
    api.set_password(password)
    api.login()

    fieldnames = ["timestamp_utc", "timestamp_local",
                  "heart_rate", "oxygen", "device_dsn"]
    rows = []

    with csv_path.open("w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        end_str = (
            end_utc.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
            if end_utc is not None
            else "Ctrl+C"
        )
        print(f"Live monitoring started. Polling every {interval}s until {end_str}.")
        print(f"Writing samples to: {csv_path}")
        print("Press Ctrl+C to stop early.")

        try:
            while end_utc is None or datetime.now(timezone.utc) < end_utc:
                loop_start = time.time()

                for device in api.get_devices():
                    if device_dsn and device.dsn != device_dsn:
                        continue
                    try:
                        device.update()
                        device.reactivate()
                    except OwletTemporaryCommunicationException:
                        continue

                    props = device.get_properties()
                    hr_prop = props.get("HEART_RATE")
                    ox_prop = props.get("OXYGEN_LEVEL")
                    if hr_prop is None or ox_prop is None:
                        continue

                    now_utc = datetime.now(timezone.utc)
                    row = {
                        "timestamp_utc": now_utc.isoformat(),
                        "timestamp_local": now_utc.astimezone(LOCAL_TIMEZONE).isoformat(),
                        "heart_rate": hr_prop.value,
                        "oxygen": ox_prop.value,
                        "device_dsn": device.dsn,
                    }
                    rows.append(row)
                    writer.writerow(row)
                    csv_file.flush()
                    print(
                        f"  {row['timestamp_local']}  "
                        f"HR={hr_prop.value} bpm  O2={ox_prop.value}%"
                    )

                elapsed = time.time() - loop_start
                sleep_time = max(0.0, interval - elapsed)
                try:
                    time.sleep(sleep_time)
                except (KeyboardInterrupt, SystemExit):
                    raise

        except KeyboardInterrupt:
            print("\nMonitoring stopped by user.")

    return rows


# ---------------------------------------------------------------------------
# CSV / plot output
# ---------------------------------------------------------------------------

def write_csv(path, rows):
    """Write *rows* (list of dicts) to a CSV at *path*."""
    if not rows:
        raise ValueError("No rows to write to CSV")
    with path.open("w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _timestamp_key(rows):
    """Return the timestamp field name present in the first row."""
    if rows and "sample_timestamp_utc" in rows[0]:
        return "sample_timestamp_utc"
    return "timestamp_utc"


def plot_metric(rows, metric, ylabel, title, output_path, tz=LOCAL_TIMEZONE):
    """Plot *metric* vs time and save the figure to *output_path*.

    Parameters
    ----------
    rows : list[dict]
        Sample rows (history or live format).
    metric : str
        Column name to plot (e.g. ``"heart_rate"``).
    ylabel : str
        Y-axis label.
    title : str
        Chart title.
    output_path : Path
        Destination PNG file.
    tz : ZoneInfo
        Timezone for x-axis labels.
    """
    try:
        os.environ.setdefault(
            "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
        os.environ.setdefault(
            "XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "fontconfig-cache"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Install matplotlib to generate plots: "
            "python3 -m pip install matplotlib"
        ) from exc

    ts_key = _timestamp_key(rows)
    timestamps = []
    values = []
    for row in rows:
        ts_str = row.get(ts_key, "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str).astimezone(tz)
        except ValueError:
            continue
        try:
            val = int(row[metric])
        except (KeyError, TypeError, ValueError):
            continue
        timestamps.append(ts)
        values.append(val)

    if not timestamps:
        raise ValueError(f"No plottable data found for metric '{metric}'")

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(timestamps, values, linewidth=1.5, marker="o", markersize=2)
    ax.set_title(title)
    ax.set_xlabel(f"Time ({tz.key})")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)
    print(f"Saved plot: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download and visualize a whole night's Owlet heart rate and "
            "oxygen data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Credentials
    parser.add_argument(
        "--email",
        help="Owlet email address. Prompts if omitted.")
    parser.add_argument(
        "--password",
        help="Owlet password. Prompts securely if omitted.")
    parser.add_argument(
        "--device",
        help="Optional device DSN filter. Uses all devices if omitted.")

    # Mode
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Live mode: poll the device every INTERVAL seconds instead of "
            "fetching stored history."
        ),
    )

    # Night window
    parser.add_argument(
        "--date",
        help=(
            "Night date in YYYY-MM-DD format (the morning that ends the "
            "overnight session). Defaults to today, i.e. last night."
        ),
    )
    parser.add_argument(
        "--start",
        help=(
            "Override night-window start as an ISO 8601 datetime, e.g. "
            "'2024-01-14T21:00'. Assumed local time if no UTC offset given."
        ),
    )
    parser.add_argument(
        "--end",
        help=(
            "Override night-window end as an ISO 8601 datetime, e.g. "
            "'2024-01-15T08:00'. Assumed local time if no UTC offset given."
        ),
    )

    # History-mode options
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help=(
            "History mode: maximum RED_ALERT_SUMMARY datapoints to request "
            "(default: 200). Each datapoint covers roughly 10 minutes; "
            "200 datapoints spans ~33 hours."
        ),
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        metavar="FILE",
        help=(
            "History mode: decode an existing history JSON file instead of "
            "calling the API. Useful for reprocessing saved downloads."
        ),
    )

    # Live-mode options
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Live mode: seconds between polls (default: 10).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        metavar="HOURS",
        help=(
            "Live mode: record for this many hours starting now "
            "(fractions allowed, e.g. 1.5 for 90 minutes). "
            "Overrides --start/--end/--date for determining the end time. "
            "Lets you record at any time of day."
        ),
    )

    # Output
    parser.add_argument(
        "--prefix",
        default="overnight_vitals",
        help="Output filename prefix used inside a timestamped run folder.",
    )
    parser.add_argument(
        "--no-plot",
        action="store_true",
        help="Skip generating PNG plots (useful in headless environments).",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Resolve the night window
    # ------------------------------------------------------------------
    if args.live and args.duration is not None:
        # Duration mode: start now, end after the requested number of hours.
        if args.duration <= 0:
            print("ERROR: --duration must be a positive number of hours.",
                  file=sys.stderr)
            sys.exit(1)
        start_utc = datetime.now(timezone.utc)
        end_utc = start_utc + timedelta(hours=args.duration)
    elif args.start and args.end:
        def _parse_local(value):
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                print(f"ERROR: Cannot parse datetime '{value}'. "
                      "Use ISO 8601 format, e.g. '2024-01-14T21:00'.",
                      file=sys.stderr)
                sys.exit(1)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TIMEZONE)
            return dt.astimezone(timezone.utc)

        start_utc = _parse_local(args.start)
        end_utc = _parse_local(args.end)
    else:
        if args.date:
            try:
                target_date = date.fromisoformat(args.date)
            except ValueError:
                print(f"ERROR: Cannot parse date '{args.date}'. "
                      "Use YYYY-MM-DD format.", file=sys.stderr)
                sys.exit(1)
        else:
            target_date = date.today()
        start_utc, end_utc = night_window_for_date(target_date)

    local_start = start_utc.astimezone(LOCAL_TIMEZONE)
    local_end = end_utc.astimezone(LOCAL_TIMEZONE)
    if args.live and args.duration is not None:
        print(
            f"Recording window: {local_start.strftime('%Y-%m-%d %H:%M %Z')} "
            f"for {args.duration:g} hour(s) "
            f"(until {local_end.strftime('%H:%M %Z')})"
        )
    else:
        print(
            f"Night window: "
            f"{local_start.strftime('%Y-%m-%d %H:%M %Z')} – "
            f"{local_end.strftime('%Y-%m-%d %H:%M %Z')}"
        )

    # ------------------------------------------------------------------
    # Output paths
    # ------------------------------------------------------------------
    run_dir = create_run_output_dir(args.prefix)
    prefix = Path(args.prefix).name
    csv_path = run_dir / f"{prefix}.csv"
    hr_plot_path = run_dir / f"{prefix}_heart_rate.png"
    ox_plot_path = run_dir / f"{prefix}_oxygen.png"
    raw_json_path = run_dir / f"{prefix}_raw_history.json"

    # ------------------------------------------------------------------
    # Collect data
    # ------------------------------------------------------------------
    if args.live:
        email = prompt_if_missing(args.email, "Owlet email: ")
        password = prompt_if_missing(args.password, "Owlet password: ", secret=True)
        rows = run_live_mode(
            email, password, args.device, args.interval, end_utc, csv_path)
        print(f"\nCollected {len(rows)} live samples.")
    else:
        if args.input_json:
            print(f"Loading history from: {args.input_json}")
        else:
            email = prompt_if_missing(args.email, "Owlet email: ")
            password = prompt_if_missing(args.password, "Owlet password: ", secret=True)
            print(f"Downloading RED_ALERT_SUMMARY history (limit={args.limit})…")

        rows, histories = run_history_mode(
            args.email if args.input_json else email,
            args.password if args.input_json else password,
            args.limit,
            args.device,
            start_utc,
            end_utc,
            input_json=args.input_json,
        )

        if not args.input_json:
            raw_json_path.write_text(
                json.dumps(histories, indent=2, default=str))
            print(f"Wrote raw history: {raw_json_path}")

        if not rows:
            print(
                "\nNo data found for the specified night window.",
                file=sys.stderr,
            )
            print(
                "Tips:\n"
                "  • Increase --limit (history mode) to fetch more history.\n"
                "  • Check --date / --start / --end matches when the sock was worn.\n"
                "  • Use --live to collect data tonight in real time.",
                file=sys.stderr,
            )
            sys.exit(1)

        write_csv(csv_path, rows)
        print(f"Wrote CSV: {csv_path}  ({len(rows)} samples in night window)")

    # ------------------------------------------------------------------
    # Guard: nothing to plot (live mode)
    # ------------------------------------------------------------------
    if not rows:
        print(
            "\nNo data found for the specified night window.",
            file=sys.stderr,
        )
        print(
            "Tips:\n"
            "  • Increase --limit (history mode) to fetch more history.\n"
            "  • Check --date / --start / --end matches when the sock was worn.\n"
            "  • Use --live to collect data tonight in real time.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Plots
    # ------------------------------------------------------------------
    window_label = (
        f"{local_start.strftime('%Y-%m-%d %H:%M')} – "
        f"{local_end.strftime('%H:%M %Z')}"
    )

    if not args.no_plot:
        plot_metric(
            rows,
            "heart_rate",
            "Heart Rate (bpm)",
            f"Heart Rate  {window_label}",
            hr_plot_path,
        )
        plot_metric(
            rows,
            "oxygen",
            "Oxygen (%)",
            f"Oxygen Level  {window_label}",
            ox_plot_path,
        )
    else:
        print("Skipping plots (--no-plot).")

    print(f"\nDone. {len(rows)} samples.")
    print(f"  Output dir:   {run_dir}")
    print(f"  CSV:          {csv_path}")
    if not args.no_plot:
        print(f"  Heart rate:   {hr_plot_path}")
        print(f"  Oxygen level: {ox_plot_path}")


if __name__ == "__main__":
    main()
