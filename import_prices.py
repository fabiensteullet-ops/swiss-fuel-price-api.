import os
import json
import logging
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("import-prices")

SOURCE_URL = os.getenv("PRICE_SOURCE_URL")
SOURCE_TOKEN = os.getenv("PRICE_SOURCE_TOKEN")

FUEL_TYPES = {"SP95", "SP98", "DIESEL"}


def fetch_prices():
    headers = {"Accept": "application/json"}

    if SOURCE_TOKEN:
        headers["Authorization"] = f"Bearer {SOURCE_TOKEN}"

    request = Request(SOURCE_URL, headers=headers)

    with urlopen(request, timeout=30) as response:
        data = json.load(response)

    if isinstance(data, dict):
        data = data.get("items")

    if not isinstance(data, list):
        raise ValueError("Format de donnees incompatible")

    return data


def validate_price(item):
    try:
        if not isinstance(item, dict):
            return False

        station_id = int(item["station_id"])
        fuel_type = item["fuel_type"]
        price = Decimal(str(item["price"]))

        return (
            station_id > 0
            and fuel_type in FUEL_TYPES
            and price.is_finite()
            and Decimal("0.50") <= price <= Decimal("5.00")
            and item.get("currency") == "CHF"
        )

    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False


def main():
    logger.info("Swiss Fuel Price - Import 1.1.0")

    if not SOURCE_URL:
        logger.info("Aucun fournisseur configure.")
        logger.info("Aucune modification des donnees.")
        return

    if not SOURCE_URL.startswith("https://"):
        raise ValueError("Une source HTTPS est obligatoire")

    prices = fetch_prices()
    valid = [item for item in prices if validate_price(item)]

    logger.info("Prix recus : %s", len(prices))
    logger.info("Prix valides : %s", len(valid))
    logger.info("Prix rejetes : %s", len(prices) - len(valid))
    logger.info("Mode test : aucune ecriture en base.")


if __name__ == "__main__":
    main()
