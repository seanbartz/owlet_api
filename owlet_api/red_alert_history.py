#!/usr/bin/env python3
"""Helpers for downloading and decoding RED_ALERT_SUMMARY history."""

from .red_alert_decoder import decode_red_alert_summary
from .red_alert_decoder import extract_json_payloads
from .owletapi import OwletAPI


def download_history(email, password, limit, device_dsn=None):
    """Download RED_ALERT_SUMMARY datapoint history for matching devices."""
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


def decode_histories(histories):
    """Decode downloaded RED_ALERT_SUMMARY histories into sample rows."""
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
