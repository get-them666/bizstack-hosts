import os

import stripe

from datetime import datetime


DEFAULT_PRICES = {
    "Turnover Cleaning": 16000,
    "Deep Cleaning": 27500,
    "Linen Restock": 7000,
    "Inspection": 9500,
}

ENV_PRICE_MAP = {
    "Turnover Cleaning": "STRIPE_PRICE_TURNOVER",
    "Deep Cleaning": "STRIPE_PRICE_DEEP",
    "Linen Restock": "STRIPE_PRICE_LINEN",
    "Inspection": "STRIPE_PRICE_INSPECTION",
}


class StripeService:
    def __init__(self):
        self.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        if self.api_key:
            stripe.api_key = self.api_key
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def get_price(self, service_type: str) -> int:
        """Return the price in cents for a service type, or 0 if unknown."""
        env_var = ENV_PRICE_MAP.get(service_type)
        if env_var:
            env_price = os.getenv(env_var)
            if env_price:
                try:
                    return int(float(env_price) * 100)
                except ValueError:
                    pass
        return DEFAULT_PRICES.get(service_type, 0)

    def create_checkout_session(
        self,
        event_id: int,
        customer_name: str,
        customer_email: str,
        service_type: str,
        start_time: datetime,
        discount_coupon: str = None,
    ) -> str:
        """Create a Stripe Checkout Session for a single booking. Returns the checkout URL."""
        if not self.is_configured():
            raise RuntimeError("STRIPE_SECRET_KEY is not configured.")

        amount_cents = self.get_price(service_type)
        if amount_cents <= 0:
            raise ValueError(f"No price configured for service type: {service_type}")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": f"Broom Service – {service_type}",
                            "description": (
                                f"Booking #{event_id} · {customer_name} · "
                                f"{start_time.strftime('%B %d, %Y at %I:%M %p')}"
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "event_id": str(event_id),
                "service_type": service_type,
                "customer_name": customer_name,
                "scheduled_start": start_time.isoformat(),
            },
            discounts=[{"coupon": discount_coupon}] if discount_coupon else None,
            customer_email=customer_email or None,
            success_url=self._base_url() + "/payments/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=self._base_url() + "/payments/cancel",
        )
        return session.url

    def construct_webhook_event(self, payload: bytes, signature: str):
        """Validate an incoming webhook and return the Stripe event object."""
        if not self.webhook_secret:
            raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured.")
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)

    def _base_url(self) -> str:
        return (os.getenv("APP_BASE_URL", "https://bizstackperks.com") or "").rstrip("/")