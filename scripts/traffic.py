"""Simple shopper that stands in for the demo's load generator.

Loops on the shop's public pages and APIs (routes read from the demo's frontend/pages/api):
home page, product list, product page, recommendations, ads, add to cart, view cart and
checkout, at about --rps requests per second.

    python scripts/traffic.py --duration 600 [--rps 2] [--base http://localhost:8080]

It never calls /feature or /flagservice. Prints request counts per route every 30 s.
"""
import argparse
import random
import time
import uuid
from collections import Counter

import requests

FORBIDDEN = ("/feature", "/flagservice")
FALLBACK_PRODUCTS = ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N"]
CATEGORIES = ["binoculars", "telescopes", "accessories", "assembly", "travel", "books"]
SHOPPER = {  # the demo's own test card details (see src/load-generator/people.json)
    "email": "shopper@example.com",
    "address": {"streetAddress": "1600 Amphitheatre Parkway", "zipCode": "94043",
                "city": "Mountain View", "state": "CA", "country": "United States"},
    "userCurrency": "USD",
    "creditCard": {"creditCardNumber": "4432-8015-6152-0454", "creditCardExpirationMonth": 1,
                   "creditCardExpirationYear": 2039, "creditCardCvv": 672},
}


class Shopper:
    def __init__(self, base, rps):
        self.base = base.rstrip("/")
        self.gap = 1.0 / rps
        self.http = requests.Session()
        self.stats = Counter()
        self.products = FALLBACK_PRODUCTS

    def call(self, method, path, route=None, **kwargs):
        if any(bad in path for bad in FORBIDDEN):
            raise RuntimeError(f"refusing forbidden path {path}")
        route = route or path
        try:
            r = self.http.request(method, self.base + path, timeout=15, **kwargs)
            self.stats[f"{route} {r.status_code}"] += 1
            return r
        except requests.RequestException as e:
            self.stats[f"{route} {type(e).__name__}"] += 1
            return None
        finally:
            time.sleep(self.gap)

    def refresh_products(self):
        r = self.call("GET", "/api/products", params={"currencyCode": "USD"})
        if r is not None and r.ok:
            ids = [p.get("id") for p in r.json() if p.get("id")]
            self.products = ids or self.products

    def visit(self):
        """One shopper session: browse, sometimes buy."""
        user = str(uuid.uuid4())
        product = random.choice(self.products)
        self.call("GET", "/")
        self.call("GET", "/api/products", params={"currencyCode": "USD"})
        self.call("GET", f"/api/products/{product}", route="/api/products/<id>",
                  params={"currencyCode": "USD"})
        self.call("GET", "/api/recommendations", params={"productIds": [product], "sessionId": user})
        self.call("GET", "/api/data/", route="/api/data", params={"contextKeys": [random.choice(CATEGORIES)]})
        if random.random() < 0.5:
            for _ in range(random.choice([1, 1, 2])):
                item = {"productId": random.choice(self.products), "quantity": random.choice([1, 2, 3])}
                self.call("POST", "/api/cart", json={"item": item, "userId": user})
            self.call("GET", "/api/cart", params={"sessionId": user, "currencyCode": "USD"})
            if random.random() < 0.6:
                self.call("POST", "/api/checkout", params={"currencyCode": "USD"},
                          json={**SHOPPER, "userId": user})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=int, default=600, help="seconds to run (0 = until Ctrl-C)")
    p.add_argument("--rps", type=float, default=2.0, help="approximate requests per second")
    p.add_argument("--base", default="http://localhost:8080")
    args = p.parse_args()

    shopper = Shopper(args.base, args.rps)
    shopper.refresh_products()
    print(f"shopping on {args.base} at ~{args.rps} req/s; {len(shopper.products)} products", flush=True)
    end = time.time() + args.duration if args.duration else float("inf")
    next_report = time.time() + 30
    try:
        while time.time() < end:
            shopper.visit()
            if time.time() >= next_report:
                print(time.strftime("%H:%M:%S"), dict(sorted(shopper.stats.items())), flush=True)
                next_report += 30
    except KeyboardInterrupt:
        pass
    print("final:", dict(sorted(shopper.stats.items())), flush=True)


if __name__ == "__main__":
    main()
