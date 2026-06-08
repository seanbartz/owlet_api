#!/usr/bin/env python

from unittest.mock import patch

from freezegun import freeze_time
from owlet_api.web_app import create_app


def test_web_app_get_form():
    app = create_app()
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"Owlet Red Alert (Past 24 Hours)" in response.data
    assert b'type="password"' in response.data


@patch("owlet_api.web_app.decode_histories")
@patch("owlet_api.web_app.download_history")
def test_web_app_filters_to_last_24_hours(mock_download_history, mock_decode_histories):
    mock_download_history.return_value = [{"device_dsn": "abc", "history": []}]
    mock_decode_histories.return_value = [
        {
            "sample_timestamp_utc": "2026-06-08T12:00:00+00:00",
            "heart_rate": 130,
            "oxygen": 98,
        },
        {
            "sample_timestamp_utc": "2026-06-07T11:00:00+00:00",
            "heart_rate": 120,
            "oxygen": 97,
        },
    ]

    app = create_app()
    client = app.test_client()

    with freeze_time("2026-06-08T14:00:00+00:00"):
        response = client.post(
            "/",
            data={"email": "user@example.com", "password": "secret", "limit": "300"},
        )

    assert response.status_code == 200
    assert b"Showing 1 decoded samples from the past 24 hours." in response.data
    assert b"Heart Rate vs Time" in response.data
    assert b"Oxygen Level vs Time" in response.data


def test_web_app_requires_credentials():
    app = create_app()
    client = app.test_client()

    response = client.post("/", data={"email": "", "password": ""})

    assert response.status_code == 200
    assert b"Email and password are required." in response.data
