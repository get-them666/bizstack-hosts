from datetime import datetime, timedelta, timezone
import os
import psycopg


class CalendarService:
    def __init__(self, db_url: str | None = None):
        self.db_url = db_url or os.getenv("DATABASE_URL")
        if not self.db_url:
            raise RuntimeError("DATABASE_URL is not configured")

    def _get_connection(self):
        return psycopg.connect(self.db_url)

    def check_availability(self, requested_time: datetime, duration_hours: int = 1) -> bool:
        if requested_time.tzinfo is None:
            requested_time = requested_time.replace(tzinfo=timezone.utc)
        end_time = requested_time + timedelta(hours=duration_hours)
        with self._get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM calendar_events
                WHERE start_time < %s AND end_time > %s
                LIMIT 1
            """, (end_time, requested_time))
            return cur.fetchone() is None

    def create_booking(self, customer_name: str, phone: str, start_time: datetime, service_type: str) -> bool:
        end_time = start_time + timedelta(hours=1)
        with self._get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM calendar_events
                WHERE start_time < %s AND end_time > %s
                LIMIT 1
            """, (end_time, start_time))
            if cur.fetchone():
                return False
            cur.execute("""
                INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type)
                VALUES (%s, %s, %s, %s, %s)
            """, (customer_name, phone, start_time, end_time, service_type))
        return True
