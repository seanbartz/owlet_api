#!/usr/bin/env python3
"""Web app for downloading and plotting recent Owlet red alert history."""

from datetime import datetime, timedelta, timezone
import math

from flask import Flask, render_template_string, request
from markupsafe import Markup

from .red_alert_history import decode_histories
from .red_alert_history import download_history


HTML_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Owlet Red Alert Viewer</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; max-width: 1000px; }
    form { display: grid; gap: 0.75rem; max-width: 420px; margin-bottom: 2rem; }
    label { font-weight: 600; display: grid; gap: 0.3rem; }
    input { padding: 0.45rem; font-size: 1rem; }
    button { padding: 0.55rem 0.9rem; width: fit-content; font-size: 1rem; }
    .error { color: #b00020; margin-bottom: 1rem; }
    .meta { margin-bottom: 1rem; color: #444; }
    .chart { margin: 1.2rem 0; border: 1px solid #ddd; padding: 0.4rem; background: #fff; }
    .caption { font-size: 0.95rem; color: #666; }
  </style>
</head>
<body>
  <h1>Owlet Red Alert (Past 24 Hours)</h1>
  <p>Credentials are submitted with POST and only used in-memory for this request.</p>
  {% if error %}
    <div class="error">{{ error }}</div>
  {% endif %}
  <form method="post" autocomplete="off">
    <label>
      Email
      <input type="email" name="email" required value="{{ email|e }}" autocomplete="username">
    </label>
    <label>
      Password
      <input type="password" name="password" required autocomplete="current-password">
    </label>
    <label>
      Device DSN (optional)
      <input type="text" name="device_dsn" value="{{ device_dsn|e }}">
    </label>
    <label>
      Max datapoints to fetch
      <input type="number" min="1" max="1000" name="limit" value="{{ limit }}">
    </label>
    <button type="submit">Download & Plot</button>
  </form>

  {% if has_results %}
    <div class="meta">
      Showing {{ sample_count }} decoded samples from the past 24 hours.
    </div>
    <div class="chart">{{ heart_rate_svg }}</div>
    <div class="chart">{{ oxygen_svg }}</div>
    <div class="caption">All times are UTC timestamps extracted from decoded red alert summary samples.</div>
  {% endif %}
</body>
</html>
"""


def _parse_timestamp(value):
    """Parse an ISO 8601 timestamp into a timezone-aware UTC datetime."""
    if not value:
        return None
    timestamp = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _within_last_24_hours(rows, now_utc):
    """Filter decoded rows to only include samples from the past 24 hours."""
    cutoff = now_utc - timedelta(hours=24)
    filtered = []
    for row in rows:
        sample_timestamp = _parse_timestamp(row.get("sample_timestamp_utc", ""))
        if sample_timestamp is None:
            sample_timestamp = _parse_timestamp(row.get("summary_timestamp_utc", ""))
        if sample_timestamp is None:
            continue
        if cutoff <= sample_timestamp <= now_utc:
            filtered.append(row)
    return filtered


def _build_series(rows, metric_name):
    """Build (timestamp, value) points for charting."""
    points = []
    for row in rows:
        timestamp = _parse_timestamp(row.get("sample_timestamp_utc", ""))
        if timestamp is None:
            continue
        try:
            value = int(row[metric_name])
        except (KeyError, TypeError, ValueError):
            continue
        points.append((timestamp, value))
    return points


def _render_svg(points, title, stroke_color):
    """Render a compact SVG line chart."""
    width = 920
    height = 260
    margin_left = 55
    margin_right = 20
    margin_top = 20
    margin_bottom = 35
    chart_width = width - margin_left - margin_right
    chart_height = height - margin_top - margin_bottom

    if not points:
        return Markup(
            "<svg width='920' height='260' role='img' aria-label='No data'>"
            "<text x='20' y='40'>No data available for chart.</text>"
            "</svg>"
        )

    min_time = min(point[0] for point in points)
    max_time = max(point[0] for point in points)
    min_value = min(point[1] for point in points)
    max_value = max(point[1] for point in points)

    if min_time == max_time:
        max_time = max_time + timedelta(seconds=1)
    if min_value == max_value:
        max_value = max_value + 1

    x_range = (max_time - min_time).total_seconds()
    y_range = float(max_value - min_value)

    polyline_points = []
    for timestamp, value in points:
        x_seconds = (timestamp - min_time).total_seconds()
        x = margin_left + (x_seconds / x_range) * chart_width
        y = margin_top + chart_height - ((value - min_value) / y_range) * chart_height
        polyline_points.append(f"{x:.2f},{y:.2f}")

    x_start = min_time.strftime("%Y-%m-%d %H:%M:%S")
    x_end = max_time.strftime("%Y-%m-%d %H:%M:%S")
    y_mid = int(math.floor((min_value + max_value) / 2))

    return Markup(
        f"<svg width='{width}' height='{height}' role='img' aria-label='{title}'>"
        f"<text x='{margin_left}' y='16' font-size='16'>{title}</text>"
        f"<line x1='{margin_left}' y1='{margin_top + chart_height}' "
        f"x2='{margin_left + chart_width}' y2='{margin_top + chart_height}' stroke='#777' />"
        f"<line x1='{margin_left}' y1='{margin_top}' x2='{margin_left}' "
        f"y2='{margin_top + chart_height}' stroke='#777' />"
        f"<polyline fill='none' stroke='{stroke_color}' stroke-width='2' "
        f"points='{' '.join(polyline_points)}' />"
        f"<text x='5' y='{margin_top + 8}' font-size='12'>{max_value}</text>"
        f"<text x='5' y='{margin_top + chart_height / 2}' font-size='12'>{y_mid}</text>"
        f"<text x='5' y='{margin_top + chart_height}' font-size='12'>{min_value}</text>"
        f"<text x='{margin_left}' y='{height - 8}' font-size='11'>{x_start}</text>"
        f"<text x='{margin_left + chart_width - 170}' y='{height - 8}' font-size='11'>{x_end}</text>"
        "</svg>"
    )


def create_app():
    """Create and configure the Flask app."""
    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def index():
        error = ""
        sample_count = 0
        heart_rate_svg = Markup("")
        oxygen_svg = Markup("")
        email = ""
        device_dsn = ""
        limit = 300

        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            device_dsn = request.form.get("device_dsn", "").strip()
            try:
                limit = int(request.form.get("limit", "300"))
            except ValueError:
                limit = 300
            limit = max(1, min(limit, 1000))

            if not email or not password:
                error = "Email and password are required."
            else:
                try:
                    histories = download_history(email, password, limit, device_dsn or None)
                    rows = decode_histories(histories)
                    recent_rows = _within_last_24_hours(rows, datetime.now(timezone.utc))
                    sample_count = len(recent_rows)
                    if sample_count == 0:
                        error = "No red alert samples were found in the past 24 hours."
                    else:
                        heart_rate_svg = _render_svg(
                            _build_series(recent_rows, "heart_rate"),
                            "Heart Rate vs Time",
                            "#3366cc",
                        )
                        oxygen_svg = _render_svg(
                            _build_series(recent_rows, "oxygen"),
                            "Oxygen Level vs Time",
                            "#0f9d58",
                        )
                except Exception:
                    error = (
                        "Unable to download red alert history right now. "
                        "Please verify your credentials and try again."
                    )

        return render_template_string(
            HTML_TEMPLATE,
            error=error,
            has_results=sample_count > 0,
            sample_count=sample_count,
            heart_rate_svg=heart_rate_svg,
            oxygen_svg=oxygen_svg,
            email=email,
            device_dsn=device_dsn,
            limit=limit,
        )

    return app


def run():
    """Run the Flask development server."""
    create_app().run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    run()
