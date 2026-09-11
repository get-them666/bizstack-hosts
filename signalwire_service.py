import os

from signalwire.rest.client import RestClient


class SignalWireService:
    """Outbound voice/SMS helper for the BizStack AI guest assistant."""

    def __init__(self):
        self.project_id = os.getenv("SIGNALWIRE_PROJECT_ID", "")
        self.api_token = os.getenv("SIGNALWIRE_API_TOKEN", "")
        self.space_url = os.getenv("SIGNALWIRE_SPACE_URL", "yourspace.signalwire.com")
        self.from_number = os.getenv("SIGNALWIRE_PHONE", "")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = RestClient(
                project=self.project_id,
                token=self.api_token,
                host=f"https://{self.space_url}",
            )
        return self._client

    def is_configured(self) -> bool:
        return bool(
            self.project_id
            and self.api_token
            and self.space_url != "yourspace.signalwire.com"
            and self.from_number
        )

    def send_sms(self, to: str, body: str) -> bool:
        """Send an outbound SMS to a guest via SignalWire."""
        if not self.is_configured() or not to or not body:
            return False
        try:
            self.client.messages.create(
                from_=self.from_number,
                to=to,
                body=body,
            )
            return True
        except Exception as e:
            print(f"❌ SignalWire SMS send failure: {e}")
            return False

    def send_link_sms(self, to: str, entry_link: str) -> bool:
        """Send a secure digital entry link mid-call."""
        body = (
            "Hi! Here is your secure entry link for your stay: "
            f"{entry_link}. Your host (BizStack Hosts) wishes you a great stay!"
        )
        return self.send_sms(to, body)
