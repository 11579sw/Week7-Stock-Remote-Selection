import unittest
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

import web


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    def login(self):
        return self.client.post(
            "/login",
            data={"username": "admin", "password": "change-me"},
        )

    def test_dashboard_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_favorite_routes_require_login(self):
        response = self.client.post("/favorites/add", data={"ticker": "AAPL"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    @patch("web.add_favorite_ticker", return_value=True)
    def test_add_favorite_normalizes_ticker(self, add_favorite_ticker):
        self.login()

        response = self.client.post(
            "/favorites/add",
            data={"ticker": "HKG: 1810"},
        )

        self.assertEqual(response.status_code, 302)
        add_favorite_ticker.assert_called_once_with("1810.HK")
        with self.client.session_transaction() as session:
            self.assertIn("已将 1810.HK 加入自选股。", session["_flashes"][0][1])

    @patch("web.remove_favorite_ticker", return_value=True)
    def test_remove_favorite_normalizes_ticker(self, remove_favorite_ticker):
        self.login()

        response = self.client.post(
            "/favorites/remove",
            data={"ticker": "hkg: 1810"},
        )

        self.assertEqual(response.status_code, 302)
        remove_favorite_ticker.assert_called_once_with("1810.HK")

    @patch("web.get_favorite_tickers", return_value=["AAPL"])
    @patch("web.make_chart", return_value="chart")
    @patch("web.fetch_stock_data")
    def test_dashboard_shows_favorites_and_quick_switch(
        self, fetch_data, make_chart, get_favorite_tickers
    ):
        self.login()
        fetch_data.return_value = {
            "ticker_symbol": "AAPL",
            "period_days": 30,
            "unit": "$",
            "currency_code": "USD",
            "close_prices": pd.Series(
                [10.0], index=pd.to_datetime(["2026-01-01"])
            ),
            "historical_data": pd.DataFrame(
                {"Close": [10.0]}, index=pd.to_datetime(["2026-01-01"])
            ),
            "fundamentals": {"longName": "Example Corp"},
            "latest_price": 10.0,
            "max_price": 10.0,
            "min_price": 10.0,
            "latest_date": "2026-01-01",
        }

        response = self.client.post(
            "/", data={"ticker": "AAPL", "days": "30"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("自选股".encode(), response.data)
        self.assertIn(b'value="AAPL"', response.data)
        self.assertIn(b'action="/"', response.data)
        get_favorite_tickers.assert_called_once()
        make_chart.assert_called_once()

    def test_health_and_service_identity_endpoints(self):
        health = self.client.get("/healthz")
        greeting = self.client.get("/api/hello")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json["status"], "ok")
        self.assertEqual(greeting.status_code, 200)
        self.assertEqual(greeting.json["service"], web.SERVICE_NAME)

    def test_socket_requires_login(self):
        client = web.socketio.test_client(web.app)

        self.assertFalse(client.is_connected())

    def test_authenticated_socket_subscribes_to_normalized_ticker(self):
        self.login()
        client = web.socketio.test_client(web.app, flask_test_client=self.client)
        with patch.object(web.socketio, "start_background_task") as start_task:
            response = client.emit(
                "subscribe_stock",
                {"ticker": "HKG: 1810"},
                callback=True,
            )
            duplicate_response = client.emit(
                "subscribe_stock",
                {"ticker": "1810.HK"},
                callback=True,
            )

        self.assertTrue(client.is_connected())
        self.assertEqual(response, {"ok": True, "ticker": "1810.HK"})
        self.assertEqual(duplicate_response, response)
        start_task.assert_called_once()
        self.assertEqual(start_task.call_args.args[1], "1810.HK")
        client.disconnect()

    def test_stock_price_worker_pushes_a_timestamped_update(self):
        stop_event = MagicMock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True
        price = {
            "ticker_symbol": "AAPL",
            "price": 12.5,
            "market_time": "2026-01-05T10:01:00",
        }

        with (
            patch("web.fetch_latest_stock_price", return_value=price),
            patch.object(web.socketio, "emit") as emit,
        ):
            web._stream_stock_prices("AAPL", stop_event)

        event_name, payload = emit.call_args.args
        self.assertEqual(event_name, "stock_price")
        self.assertEqual(payload["ticker_symbol"], "AAPL")
        self.assertEqual(payload["price"], 12.5)
        self.assertEqual(payload["market_time"], "2026-01-05T10:01:00")
        self.assertIn("updated_at", payload)
        self.assertEqual(emit.call_args.kwargs["to"], "stock:AAPL")

    def test_database_test_creates_updates_and_reads_a_row(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = (
            web.SERVICE_NAME,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        fake_psycopg = SimpleNamespace(
            connect=MagicMock(return_value=connection),
            Error=RuntimeError,
        )

        with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            response = self.client.get("/db-test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")
        self.assertEqual(response.json["database_service"], web.SERVICE_NAME)
        self.assertEqual(cursor.execute.call_count, 3)

    def test_favorite_storage_initializes_table_and_reads_and_writes_rows(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        connection.cursor.return_value = cursor
        cursor.fetchall.return_value = [("AAPL",)]
        cursor.fetchone.side_effect = [("MSFT",), ("MSFT",)]
        fake_psycopg = SimpleNamespace(
            connect=MagicMock(return_value=connection),
            Error=RuntimeError,
        )

        with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            favorites = web.get_favorite_tickers()
            added = web.add_favorite_ticker("MSFT")
            removed = web.remove_favorite_ticker("MSFT")

        self.assertEqual(favorites, ["AAPL"])
        self.assertTrue(added)
        self.assertTrue(removed)
        self.assertEqual(fake_psycopg.connect.call_count, 3)
        self.assertEqual(cursor.execute.call_count, 6)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS favorite_stocks",
            cursor.execute.call_args_list[0].args[0],
        )
        self.assertIn(
            "ON CONFLICT",
            cursor.execute.call_args_list[3].args[0],
        )
        self.assertIn(
            "DELETE FROM favorite_stocks",
            cursor.execute.call_args_list[5].args[0],
        )

    @patch("web.get_favorite_tickers", return_value=[])
    @patch("web.make_chart", return_value="chart")
    @patch("web.fetch_stock_data")
    def test_login_and_query(self, fetch_data, make_chart, get_favorite_tickers):
        fetch_data.return_value = {
            "ticker_symbol": "AAPL",
            "period_days": 30,
            "unit": "$",
            "currency_code": "USD",
            "close_prices": pd.Series(
                [10.0], index=pd.to_datetime(["2026-01-01"])
            ),
            "historical_data": pd.DataFrame(
                {"Close": [10.0]}, index=pd.to_datetime(["2026-01-01"])
            ),
            "fundamentals": {"longName": "Example Corp"},
            "latest_price": 10.0,
            "max_price": 10.0,
            "min_price": 10.0,
            "latest_date": "2026-01-01",
        }

        login_response = self.login()
        self.assertEqual(login_response.status_code, 302)
        response = self.client.post(
            "/", data={"ticker": "AAPL", "days": "30"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Example Corp", response.data)
        self.assertIn(b"cdn.socket.io/4.8.1/socket.io.min.js", response.data)
        self.assertIn(b"subscribe_stock", response.data)
        fetch_data.assert_called_once_with("AAPL", 30)
        make_chart.assert_called_once()


if __name__ == "__main__":
    unittest.main()
