#!/usr/bin/env python3
"""Decode base64 summary properties from Owlet attributes output."""

import argparse
import base64
import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BASE64_PROPERTY_RE = re.compile(
    r"^(MONITORING_SUMMARY|RED_ALERT_SUMMARY)\s+.*?\s([A-Za-z0-9+/=]+)$",
    re.MULTILINE,
)
TIMESTAMP_RE = re.compile(r"20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\+00:00|Z)?")
LOCAL_TIMEZONE = ZoneInfo("America/New_York")


def read_varint(data, offset):
    value = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7


def parse_protobuf_message(data):
    fields = []
    offset = 0
    while offset < len(data):
        start = offset
        tag, offset = read_varint(data, offset)
        field = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            value = data[offset:offset + length]
            offset += length
        else:
            value = data[offset:]
            offset = len(data)
        fields.append((start, field, wire_type, value))
    return fields


def extract_base64_properties(text):
    return {
        match.group(1): base64.b64decode(match.group(2))
        for match in BASE64_PROPERTY_RE.finditer(text)
    }


def parse_timestamp(value):
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
    if not isinstance(value, str) or len(value) < 16:
        return None

    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


def extract_text_payloads(text):
    payloads = []
    for match in BASE64_PROPERTY_RE.finditer(text):
        line = match.group(0)
        timestamp_match = TIMESTAMP_RE.search(line)
        timestamp_utc, timestamp_local = parse_timestamp(
            timestamp_match.group(0) if timestamp_match else "")
        payloads.append({
            "name": match.group(1),
            "source": "attributes",
            "timestamp_utc": timestamp_utc,
            "timestamp_local": timestamp_local,
            "data": base64.b64decode(match.group(2)),
        })
    return payloads


def extract_json_payloads(value):
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
                    next_context.get("timestamp", ""))
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


def extract_summary_payloads(text):
    try:
        return extract_json_payloads(json.loads(text))
    except json.JSONDecodeError:
        return extract_text_payloads(text)


def decode_red_alert_summary(data, metadata=None):
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
            sample_timestamp = sample_start_utc + timedelta(seconds=(sample_index - 1) * 10)
            sample_timestamp_utc = sample_timestamp.isoformat()
            sample_timestamp_local = sample_timestamp.astimezone(LOCAL_TIMEZONE).isoformat()
            sample_time_local = sample_timestamp.astimezone(LOCAL_TIMEZONE).strftime("%H:%M:%S")
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


def decode_monitoring_summary(data):
    rows = []
    for offset, byte in enumerate(data):
        if byte != 0x08:
            continue

        try:
            timestamp, field_offset = read_varint(data, offset + 1)
        except IndexError:
            continue

        if not 1700000000 <= timestamp <= 1900000000:
            continue

        fields = {
            "timestamp": timestamp,
            "interval_seconds": "",
            "field_7": "",
            "summary_values": "",
        }
        parse_offset = field_offset
        try:
            while parse_offset < len(data):
                tag, parse_offset = read_varint(data, parse_offset)
                if tag == 0:
                    break

                field = tag >> 3
                wire_type = tag & 7
                if wire_type == 0:
                    value, parse_offset = read_varint(data, parse_offset)
                elif wire_type == 2:
                    length, parse_offset = read_varint(data, parse_offset)
                    value = data[parse_offset:parse_offset + length]
                    parse_offset += length
                else:
                    break

                if field == 4 and wire_type == 0:
                    fields["interval_seconds"] = value
                elif field == 7 and wire_type == 0:
                    fields["field_7"] = value
                elif field == 36 and wire_type == 2:
                    fields["summary_values"] = " ".join(str(byte) for byte in value)
                    break
        except (IndexError, ValueError):
            continue

        timestamp_utc = datetime.fromtimestamp(timestamp, timezone.utc)
        rows.append({
            "offset": offset,
            "timestamp_utc": timestamp_utc.isoformat(),
            "timestamp_local": timestamp_utc.astimezone(LOCAL_TIMEZONE).isoformat(),
            "timestamp": fields["timestamp"],
            "interval_seconds": fields["interval_seconds"],
            "field_7": fields["field_7"],
            "summary_values": fields["summary_values"],
            "record_hex": data[offset:parse_offset].hex(" "),
        })

    return rows


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Decode Owlet attributes summaries.")
    parser.add_argument("input", type=Path, help="Attributes text or history JSON")
    parser.add_argument("--prefix", default="decoded_attributes", help="Output CSV filename prefix")
    args = parser.parse_args()

    payloads = extract_summary_payloads(args.input.read_text())
    red_payloads = [
        payload for payload in payloads
        if payload["name"] == "RED_ALERT_SUMMARY" or decode_red_alert_summary(payload["data"])
    ]
    if red_payloads:
        rows = []
        for payload_index, payload in enumerate(red_payloads, start=1):
            rows.extend(decode_red_alert_summary(payload["data"], {
                "summary_index": payload_index,
                "summary_source": payload["source"],
                "summary_timestamp_utc": payload["timestamp_utc"],
                "summary_timestamp_local": payload["timestamp_local"],
            }))
        write_csv(Path(f"{args.prefix}_red_alert_summary.csv"), rows)
        print(f"RED_ALERT_SUMMARY payloads: {len(red_payloads)} rows: {len(rows)}")

    monitoring_payloads = [
        payload for payload in payloads
        if payload["name"] == "MONITORING_SUMMARY"
    ]
    if monitoring_payloads:
        rows = []
        for payload in monitoring_payloads:
            rows.extend(decode_monitoring_summary(payload["data"]))
        write_csv(Path(f"{args.prefix}_monitoring_summary.csv"), rows)
        print(f"MONITORING_SUMMARY payloads: {len(monitoring_payloads)} rows: {len(rows)}")


if __name__ == "__main__":
    main()
