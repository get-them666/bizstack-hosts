import json
import os
from datetime import datetime
from typing import Optional

from openai import OpenAI


class BusinessAIAgent:
    """Conversational assistant that runs BizStack Hosts.

    Uses a knowledge base plus OpenAI tool calling so it can answer questions
    about the company, hospitality/STR industry, and the site itself, while also
    checking availability, creating bookings, sending Stripe payment links,
    logging customers, and reporting business stats.
    """

    def __init__(
        self,
        knowledge_path: str = "bot_knowledge.md",
        tool_handlers: Optional[dict] = None,
        model: Optional[str] = None,
    ):
        self._client = None
        self._knowledge = self._load_knowledge(knowledge_path)
        self._tool_handlers = tool_handlers or {}
        self._model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    @staticmethod
    def _load_knowledge(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured.")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def _build_system_prompt(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        knowledge = self._knowledge or (
            "You are the automated representative for BizStack Hosts, a short-term "
            "rental turnover cleaning and co-hosting company. Be concise and professional."
        )
        return f"""
You are the automated AI assistant that runs BizStack Hosts.
Current time: {now}.
You must answer strictly from the knowledge base below. Never invent prices,
availability, policies, or URLs.

HOW TO SOUND LIKE A REAL HUMAN (non-negotiables):
- Write the way a friendly, sharp human assistant texts: contractions, short punchy
  sentences, and a natural rhythm. Vary sentence length. Never sound robotic, canned,
  or like a script.
- Open naturally based on context: a quick hey, a friendly confirmation, or a warm
  "Got it —". No "Greetings!", no "As an AI", no corporate boilerplate.
- One thought per text: break things into short lines instead of giant walls of text.
  Keep total replies short — a guest reads these on a phone.
- Use light, human details ("Perfect — Friday at 2 works!"), but stay truthful. Never
  invent facts. If you don't know, say so plainly ("Let me confirm that for you.").
- It's fine to be warm and a little personality-driven, but never over-the-top, never
  use 10 emojis, and never pretend to be a specific named person. You are the BizStack
  Hosts assistant.
- When confirming a booking, mirror real human confirmation style: restate the
  details simply and tell them exactly what happens next ("I've got you locked in for
  Friday at 2 — here's your secure payment link to confirm").

KNOWLEDGE BASE:
{knowledge}

TOOL USAGE RULES:
- Use check_booking_availability before suggesting or confirming any time slot.
- Use create_booking ONLY after the guest has confirmed name, service, date, and time.
- Always follow a successful create_booking by giving the guest the payment link.
- Use lookup_bookings whenever a guest asks about their existing bookings.
- Use register_customer when a new guest/prospect shares their info and you have no
  existing booking for them yet.
- Use get_business_summary when the owner asks how the business is doing.
- If a tool returns an error or an unavailable slot, respond helpfully and offer
  the nearest open time from the availability result.
- When checking availability or creating bookings, you MUST convert the guest's
  requested date/time to an ISO-8601 local datetime string like 2026-09-18T14:00:00.
  Assume US Eastern time (America/New_York) when the guest gives a date without a zone.
"""

    def _tools(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "check_booking_availability",
                    "description": (
                        "Check whether a requested start time is open for a cleaning "
                        "operation. Returns open/conflict status and the nearest open "
                        "slots around the requested time."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_time": {
                                "type": "string",
                                "description": (
                                    "ISO-8601 local datetime, e.g. 2026-09-18T14:00:00. "
                                    "Never guess times from booking queries you cannot parse — "
                                    "ask the guest to confirm the date and time."
                                ),
                            }
                        },
                        "required": ["start_time"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_booking",
                    "description": (
                        "Create a confirmed booking and generate the guest's secure Stripe "
                        "payment link. ONLY use after the guest has explicitly confirmed "
                        "their name, service type, date, and time."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "customer_name": {
                                "type": "string",
                                "description": "Guest's full name.",
                            },
                            "phone": {
                                "type": "string",
                                "description": "Guest's phone number, E.164 if possible (e.g. +19482316699).",
                            },
                            "service_type": {
                                "type": "string",
                                "enum": ["Turnover Cleaning", "Deep Cleaning", "Linen Restock", "Inspection"],
                                "description": "The service being booked.",
                            },
                            "start_time": {
                                "type": "string",
                                "description": "ISO-8601 local datetime, e.g. 2026-09-18T14:00:00.",
                            },
                        },
                        "required": ["customer_name", "phone", "service_type", "start_time"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_bookings",
                    "description": "Look up a guest's bookings by phone number, newest first.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "phone": {
                                "type": "string",
                                "description": "Phone number used for the booking.",
                            }
                        },
                        "required": ["phone"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "register_customer",
                    "description": "Save a new customer or prospect record with their contact details.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Customer's name."},
                            "email": {"type": "string", "description": "Customer's email, if provided."},
                            "phone": {"type": "string", "description": "Customer's phone number, if provided."},
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_business_summary",
                    "description": "Return current business stats: hosts, customers, bookings, and leads counts.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    def _execute_tool(self, name: str, arguments: str) -> str:
        handler = self._tool_handlers.get(name)
        if handler is None:
            return json.dumps({"ok": False, "error": f"No handler for tool: {name}"})
        try:
            args = json.loads(arguments or "{}") if isinstance(arguments, str) else (arguments or {})
            result = handler(**args)
            if not isinstance(result, (str, bytes)):
                result = json.dumps(result)
            return result
        except TypeError as e:
            return json.dumps({"ok": False, "error": f"Invalid tool arguments: {e}"})
        except Exception as e:
            print(f"⚠️ Tool {name} failed: {e}")
            return json.dumps({"ok": False, "error": str(e)})

    def process_inbound_text(self, context_stream: str) -> str:
        """Handle an inbound SMS/voice message end-to-end (with tool calling if wired)."""
        fallback = "Message received. Our team will follow up with you shortly."
        if not os.getenv("OPENAI_API_KEY"):
            return fallback

        if not self._tool_handlers:
            return self._simple_reply(context_stream)

        messages: list = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": context_stream},
        ]

        try:
            for _ in range(5):
                response = self.client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=self._tools(),
                    tool_choice="auto",
                    max_tokens=300,
                    temperature=0.7,
                )
                message = response.choices[0].message
                if not message.tool_calls:
                    return (message.content or "").strip() or fallback

                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in message.tool_calls
                        ],
                    }
                )
                for tc in message.tool_calls:
                    tool_output = self._execute_tool(tc.function.name, tc.function.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_output,
                        }
                    )
            return fallback
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return fallback

    def _simple_reply(self, context_stream: str) -> str:
        fallback = "Message received. Our team will follow up with you shortly."
        try:
            response = self.client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": context_stream},
                ],
                max_tokens=300,
                temperature=0.7,
            )
            return response.choices[0].message.content or fallback
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return fallback