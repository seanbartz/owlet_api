#!/usr/bin/env python3
"""Decode RED_ALERT_SUMMARY payloads from Owlet history responses."""

import base64
from datetime import datetime, timedelta, timezone


LOCAL_TIMEZONE = timezone.utc


def parse_timestamp(value):
    """Parse timestamp text and return UTC and local ISO strings."""
    if not value:
        return "", ""

    timestamp = value.replace(" ", "T").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return value, ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.isoformat(), parsed.astimezone(LOCAL_TIMEZONE).isoformat()


def try_base64_decode(value):
    """Try to decode a base64 string; return None when invalid."""
    if not isinstance(value, str) or len(value) < 16:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


def extract_json_payloads(value):
    """Extract deduplicated RED_ALERT_SUMMARY payloads from history JSON."""
    payloads = []
    seen = set()

    def walk(node, context):
        if isinstance(node, list):
            for item in node:
                walk(item, context)
            return

        if not isinstance(node, dict):
            return

        next_context = dict(context)
        for key in ("name", "display_name"):
            if key in node and isinstance(node[key], str):
                next_context[key] = node[key]
        for key in ("created_at", "updated_at", "data_updated_at", "timestamp"):
            if key in node and isinstance(node[key], str):
                next_context["timestamp"] = node[key]

        if "datapoint" in node:
            walk(node["datapoint"], next_context)

        decoded = try_base64_decode(node.get("value"))
        if decoded is not None:
            rows = decode_red_alert_summary(decoded)
            fingerprint = (node.get("value"), next_context.get("timestamp", ""))
            if rows and fingerprint not in seen:
                seen.add(fingerprint)
                timestamp_utc, timestamp_local = parse_timestamp(
                    next_context.get("timestamp", "")
                )
                payloads.append({
                    "name": next_context.get("name", "RED_ALERT_SUMMARY"),
                    "source": "history",
                    "timestamp_utc": timestamp_utc,
                    "timestamp_local": timestamp_local,
                    "data": decoded,
                })

        for child in node.values():
            if isinstance(child, (dict, list)):
                walk(child, next_context)

    walk(value, {})
    return payloads


def decode_red_alert_summary(data, metadata=None):
    """Decode sample rows from binary RED_ALERT_SUMMARY payload bytes."""
    metadata = metadata or {}
    rows = []
    best_offset = None
    best_count = -1

    for offset in range(min(20, len(data))):
        count = 0
        valid_first = False
        for index in range(offset, len(data) - 4, 5):
            marker, heart_rate, oxygen, status, tail = data[index:index + 5]
            if marker == 0 and 40 <= heart_rate <= 220 and 40 <= oxygen <= 100 and tail == 6:
                count += 1
                if index == offset:
                    valid_first = True
        if count > best_count or (count == best_count and valid_first):
            best_offset = offset
            best_count = count

    if best_offset is None:
        return rows

    header = data[:best_offset]
    sample_start_utc = None
    if len(header) >= 8:
        candidate_timestamp = int.from_bytes(header[4:8], "big")
        if 1700000000 <= candidate_timestamp <= 1900000000:
            sample_start_utc = datetime.fromtimestamp(candidate_timestamp, timezone.utc)

    sample_index = 1
    for index in range(best_offset, len(data) - 4, 5):
        marker, heart_rate, oxygen, status, tail = data[index:index + 5]
        if marker != 0 or tail != 6:
            continue
        sample_timestamp_utc = ""
        sample_timestamp_local = ""
        sample_time_local = ""
        if sample_start_utc is not None:
            sample_timestamp = sample_start_utc + timedelta(
                seconds=(sample_index - 1) * 10
            )
            sample_timestamp_utc = sample_timestamp.isoformat()
            sample_timestamp_local = sample_timestamp.astimezone(LOCAL_TIMEZONE).isoformat()
            sample_time_local = sample_timestamp.astimezone(LOCAL_TIMEZONE).strftime(
                "%H:%M:%S"
            )
        row = {
            "sample": sample_index,
            "sample_timestamp_utc": sample_timestamp_utc,
            "sample_timestamp_local": sample_timestamp_local,
            "sample_time_local": sample_time_local,
            "offset": index,
            "heart_rate": heart_rate,
            "oxygen": oxygen,
            "status": status,
            "tail": tail,
            "header_hex": header.hex(" "),
        }
        row.update(metadata)
        rows.append(row)
        sample_index += 1

    return rows
