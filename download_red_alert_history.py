#!/usr/bin/env python3
"""Download Red Alert history, decode it, and plot HR/O2 over time."""

import argparse
import csv
import getpass
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from decode_owlet_attributes import decode_red_alert_summary
from decode_owlet_attributes import extract_json_payloads
from owlet_api.owletapi import OwletAPI


LOCAL_TIMEZONE = ZoneInfo("America/New_York")


def prompt_if_missing(value, prompt, secret=False):
    if value:
        return value
    if secret:
        return getpass.getpass(prompt)
    return input(prompt)


def download_history(email, password, limit, device_dsn=None):
    api = OwletAPI()
    api.set_email(email)
    api.set_password(password)
    api.login()

    histories = []
    for device in api.get_devices():
        if device_dsn is not None and device.dsn != device_dsn:
            continue
        device.update()
        histories.append({
            "device_dsn": device.dsn,
            "history": device.get_property_datapoints("RED_ALERT_SUMMARY", limit),
        })

    return histories


def load_histories(path):
    value = json.loads(path.read_text())
    if isinstance(value, list) and all(isinstance(item, dict) and "history" in item for item in value):
        return value
    return [{"device_dsn": "", "history": value}]


def decode_histories(histories):
    rows = []
    summary_index = 1
    for history in histories:
        device_dsn = history.get("device_dsn", "")
        payloads = extract_json_payloads(history["history"])
        for payload in payloads:
            decoded_rows = decode_red_alert_summary(payload["data"], {
                "summary_index": summary_index,
                "summary_source": payload["source"],
                "summary_timestamp_utc": payload["timestamp_utc"],
                "summary_timestamp_local": payload["timestamp_local"],
                "device_dsn": device_dsn,
            })
            rows.extend(decoded_rows)
            summary_index += 1

    return sorted(rows, key=lambda row: row["sample_timestamp_utc"])


def write_csv(path, rows):
    if not rows:
        raise ValueError("No RED_ALERT_SUMMARY rows found")

    with path.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plot_metric(rows, metric, ylabel, title, output_path):
    try:
        os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
        os.environ.setdefault("XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "fontconfig-cache"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("Install matplotlib to generate plots: python3 -m pip install matplotlib") from error

    timestamps = [
        datetime.fromisoformat(row["sample_timestamp_utc"]).astimezone(LOCAL_TIMEZONE)
        for row in rows
        if row["sample_timestamp_utc"]
    ]
    values = [
        int(row[metric])
        for row in rows
        if row["sample_timestamp_utc"]
    ]

    if not timestamps:
        raise ValueError("No sample timestamps found for plotting")

    fig, axis = plt.subplots(figsize=(12, 5))
    axis.plot(timestamps, values, linewidth=1.8, marker="o", markersize=2.5)
    axis.set_title(title)
    axis.set_xlabel("Time (EDT)")
    axis.set_ylabel(ylabel)
    axis.grid(True, alpha=0.3)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=LOCAL_TIMEZONE))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="Download and decode Owlet RED_ALERT_SUMMARY history.")
    parser.add_argument("--email", help="Owlet email. Prompts if omitted.")
    parser.add_argument("--password", help="Owlet password. Prompts securely if omitted.")
    parser.add_argument("--device", help="Optional device DSN filter.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum datapoints to request.")
    parser.add_argument("--prefix", default="red_alert_history", help="Output filename prefix.")
    parser.add_argument("--input-json", type=Path, help="Decode an existing history JSON file.")
    args = parser.parse_args()

    prefix = Path(args.prefix)
    raw_json_path = prefix.with_name(prefix.name + ".json")
    csv_path = prefix.with_name(prefix.name + "_red_alert_summary.csv")
    heart_rate_plot_path = prefix.with_name(prefix.name + "_heart_rate.png")
    oxygen_plot_path = prefix.with_name(prefix.name + "_oxygen.png")

    if args.input_json:
        histories = load_histories(args.input_json)
    else:
        email = prompt_if_missing(args.email, "Owlet email: ")
        password = prompt_if_missing(args.password, "Owlet password: ", secret=True)
        histories = download_history(email, password, args.limit, args.device)
        raw_json_path.write_text(json.dumps(histories, indent=2, default=str))

    rows = decode_histories(histories)
    write_csv(csv_path, rows)
    plot_metric(rows, "heart_rate", "Heart Rate (bpm)", "Owlet Red Alert Heart Rate", heart_rate_plot_path)
    plot_metric(rows, "oxygen", "Oxygen (%)", "Owlet Red Alert Oxygen", oxygen_plot_path)

    print(f"Decoded rows: {len(rows)}")
    if not args.input_json:
        print(f"Wrote raw history: {raw_json_path}")
    print(f"Wrote CSV: {csv_path}")
    print(f"Wrote heart-rate plot: {heart_rate_plot_path}")
    print(f"Wrote oxygen plot: {oxygen_plot_path}")


if __name__ == "__main__":
    main()
