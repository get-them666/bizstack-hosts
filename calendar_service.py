import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta
import os

class CalendarService:
    def __init__(self):
        self.db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/bizstack")

    def _get_connection(self):
        return psycopg.connect(self.db_url, row_factory=dict_row)

    def check_availability(self, requested_time: datetime) -> bool:
        """Verify if a specific timestamp is completely clear of existing operations."""
        query = "SELECT COUNT(*) FROM calendar_events WHERE start_time = %s;"
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (requested_time,))
                result = cur.fetchone()
                return result['count'] == 0

    def create_booking(self, customer_name: str, phone: str, start_time: datetime, service_type: str) -> bool:
        """Persist a newly closed phone or text booking sequence."""
        if not self.check_availability(start_time):
            return False

        end_time = start_time + timedelta(hours=1)
        query = """
            INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type)
            VALUES (%s, %s, %s, %s, %s);
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (customer_name, phone, start_time, end_time, service_type))
                conn.commit()
        return True
