#!/usr/bin/env python3
"""Decode Owlet Dream Sock VITALS_LOG_FILE-style raw logs to CSV."""

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path


def find_record_starts(data):
    """Find compact record starts and embedded timestamp anchors."""
    starts = []
    anchors = {}
    index = 0

    while index < len(data) - 6:
        if data[index] == 0x0C and data[index + 1] in (0x69, 0x6A):
            timestamp = int.from_bytes(data[index + 1:index + 5], "big")
            if 1_700_000_000 <= timestamp <= 1_900_000_000:
                start = index + 5
                starts.append(start)
                anchors[start] = timestamp
                index += 6
                continue

        if data[index] == 0x08:
            starts.append(index)
            index += 2
            continue

        index += 1

    return sorted(set(starts)), anchors


def parse_zero_prefixed_values(body):
    """Parse the observed body shape: optional status bytes, then 00 value pairs."""
    status = []
    index = 0
    while index < len(body) and body[index] != 0:
        status.append(body[index])
        index += 1

    values = []
    while index + 1 < len(body) and body[index] == 0:
        values.append(body[index + 1])
        index += 2

    return status, values, body[index:]


def decode_records(data):
    starts, anchors = find_record_starts(data)
    records = []
    current_anchor_record = None
    current_anchor_timestamp = None

    for ordinal, start in enumerate(starts):
        end = starts[ordinal + 1] if ordinal + 1 < len(starts) else min(len(data), start + 64)
        raw_record = data[start:end]
        if len(raw_record) < 2:
            continue

        if raw_record[0] == 0x08:
            counter = raw_record[1]
            body = raw_record[2:]
        else:
            counter = raw_record[0]
            body = raw_record[1:]

        if start in anchors:
            current_anchor_record = ordinal
            current_anchor_timestamp = anchors[start]

        estimated_timestamp = None
        if current_anchor_record is not None:
            estimated_timestamp = current_anchor_timestamp + (ordinal - current_anchor_record)

        status, values, remainder = parse_zero_prefixed_values(body)
        heart_rate = values[0] if len(values) > 0 and 80 <= values[0] <= 220 else None
        oxygen = values[1] if len(values) > 1 and 50 <= values[1] <= 105 else None
        quality = values[2] if len(values) > 2 else None

        records.append({
            "ordinal": ordinal,
            "offset": start,
            "counter": counter,
            "timestamp_utc": (
                datetime.fromtimestamp(estimated_timestamp, timezone.utc).isoformat()
                if estimated_timestamp is not None else ""
            ),
            "status": " ".join(f"{value:02x}" for value in status),
            "values": " ".join(str(value) for value in values),
            "heart_rate": heart_rate if heart_rate is not None else "",
            "oxygen": oxygen if oxygen is not None else "",
            "quality": quality if quality is not None else "",
            "remainder_hex": remainder.hex(" "),
            "record_hex": raw_record.hex(" "),
        })

    return records


def main():
    parser = argparse.ArgumentParser(description="Decode Owlet raw vitals log to CSV.")
    parser.add_argument("input", type=Path, help="Raw log file, for example logged_data.raw")
    parser.add_argument("-o", "--output", type=Path, default=Path("decoded_owlet_log.csv"))
    parser.add_argument("--oxygen", type=int, help="Print rows where candidate oxygen equals this value")
    args = parser.parse_args()

    records = decode_records(args.input.read_bytes())
    fieldnames = [
        "ordinal",
        "offset",
        "counter",
        "timestamp_utc",
        "status",
        "values",
        "heart_rate",
        "oxygen",
        "quality",
        "remainder_hex",
        "record_hex",
    ]

    with args.output.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    if args.oxygen is not None:
        for record in records:
            if record["oxygen"] == args.oxygen:
                print(
                    f'{record["timestamp_utc"]} '
                    f'hr={record["heart_rate"]} oxygen={record["oxygen"]} '
                    f'values={record["values"]} offset={record["offset"]}'
                )

    print(f"Wrote {len(records)} records to {args.output}")


if __name__ == "__main__":
    main()
