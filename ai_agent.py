from openai import OpenAI
from datetime import datetime
import os


class BusinessAIAgent:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured.")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def process_inbound_text(self, context_stream: str) -> str:
        """Processes raw text inputs (SMS or Email) to classify user intent and format output."""
        system_prompt = f"""
You are the automated booking representative for BizStack Hosts.
Analyze the incoming message and generate a highly concise, professional reply.
Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.
If they want to book an STR operation or turnover cleaning, provide instructions or confirm availability.
"""

        try:
            response = self.client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": context_stream},
                ],
                max_tokens=150,
            )
            return response.choices[0].message.content
        except RuntimeError:
            return "Message received. Our team will follow up with you shortly."
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return "Message received. Our team will follow up with you shortly."