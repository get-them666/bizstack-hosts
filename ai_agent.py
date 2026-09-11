import os
from datetime import datetime, timezone


class BusinessAIAgent:
    """Optional OpenAI helper. The core web app does not depend on OpenAI being configured."""

    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")

    def process_inbound_text(self, context_stream: str) -> str:
        if not self.api_key:
            return "Thanks for contacting BizStack Hosts. We received your message and will follow up shortly."
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "You are the concise booking representative for BizStack Hosts. Help with short-term-rental operations and turnover scheduling."},
                {"role": "user", "content": context_stream},
            ],
            max_tokens=180,
        )
        return response.choices[0].message.content or "Thanks for contacting BizStack Hosts."
