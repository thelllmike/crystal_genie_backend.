"""One-off: create the $3.69/month Crystal Genie Premium price in Stripe.

    python create_stripe_price.py

Prints the price id to paste into STRIPE_PRICE_ID in the backend's .env. Re-running
reuses the existing product/price instead of creating duplicates — it looks
them up by the lookup key below.

NOTE: this hits whichever Stripe account STRIPE_SECRET_KEY points at. With a
live key it creates a real, live product.
"""

import os

import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

LOOKUP_KEY = "crystal_genie_premium_monthly"
AMOUNT_CENTS = 369
CURRENCY = os.getenv("STRIPE_CURRENCY", "usd")


def main() -> None:
    mode = "LIVE" if stripe.api_key.startswith("sk_live") else "TEST"
    print(f"Stripe mode: {mode}")

    existing = stripe.Price.list(lookup_keys=[LOOKUP_KEY], limit=1).data
    if existing:
        print(f"Price already exists: {existing[0].id}")
        print(f"\nSTRIPE_PRICE_ID={existing[0].id}")
        return

    product = stripe.Product.create(
        name="Crystal Genie Premium",
        description="Unlimited crystal scanning",
    )
    price = stripe.Price.create(
        product=product.id,
        unit_amount=AMOUNT_CENTS,
        currency=CURRENCY,
        recurring={"interval": "month"},
        lookup_key=LOOKUP_KEY,
    )
    print(f"Created product {product.id} and price {price.id}")
    print(f"\nSTRIPE_PRICE_ID={price.id}")


if __name__ == "__main__":
    main()
