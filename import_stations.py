
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error


API_URL = os.environ["API_URL"].rstrip("/")
IMPORT_SECRET = os.environ["IMPORT_SECRET"]

if not IMPORT_SECRET:
    raise RuntimeError("IMPORT_SECRET manquant")

BATCH_SIZE = 100

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

QUERY = """
[out:json][timeout:120];

area["ISO3166-1"="CH"]
    ["admin_level"="2"]->.ch;

(
    node["amenity"="fuel"](area.ch);
    way["amenity"="fuel"](area.ch);
    relation["amenity"="fuel"](area.ch);
);

out center tags;
"""


def fetch_stations():

    data = urllib.parse.urlencode({
        "data": QUERY
    }).encode("utf-8")

    for server in OVERPASS_SERVERS:

        try:
            print(
                f"Connexion Overpass : {server}",
                flush=True
            )

            request = urllib.request.Request(
                server,
                data=data,
                headers={
                    "User-Agent":
                        "SwissFuelPrice/0.5.0",
                    "Content-Type":
                        "application/x-www-form-urlencoded"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=180
            ) as response:

                result = json.loads(
                    response.read().decode("utf-8")
                )

            if result.get("remark"):
                raise RuntimeError(
                    "Reponse Overpass incomplete"
                )

            elements = result.get("elements", [])

            if not elements:
                raise RuntimeError(
                    "Aucune station recue"
                )

            print(
                f"{len(elements)} objets recuperes",
                flush=True
            )

            return elements

        except Exception as error:

            print(
                f"Echec Overpass : {error}",
                flush=True
            )

            time.sleep(5)

    raise RuntimeError(
        "Aucun serveur Overpass disponible"
    )


def clean(value, limit):

    if not value:
        return None

    return str(value)[:limit]


def prepare_stations(elements):

    stations = {}

    for element in elements:

        tags = element.get("tags", {})

        osm_type = element.get("type")
        osm_id = element.get("id")

        if osm_type not in (
            "node", "way", "relation"
        ):
            continue

        if not osm_id:
            continue

        if osm_type == "node":

            latitude = element.get("lat")
            longitude = element.get("lon")

        else:

            center = element.get("center", {})

            latitude = center.get("lat")
            longitude = center.get("lon")

        if latitude is None or longitude is None:
            continue

        name = (
            tags.get("name")
            or tags.get("brand")
            or tags.get("operator")
            or "Station sans nom"
        )

        address = " ".join(
            value for value in [
                tags.get("addr:street"),
                tags.get("addr:housenumber")
            ]
            if value
        )

        city = (
            tags.get("addr:city")
            or tags.get("addr:town")
            or tags.get("addr:village")
        )

        station = {
            "osm_id": osm_id,
            "osm_type": osm_type,
            "name": clean(name, 200),
            "brand": clean(
                tags.get("brand"), 100
            ),
            "address": clean(address, 250),
            "postal_code": clean(
                tags.get("addr:postcode"), 10
            ),
            "city": clean(city, 100),
            "latitude": latitude,
            "longitude": longitude
        }

        stations[(osm_type, osm_id)] = station

    return list(stations.values())


def send_batch(batch):

    payload = json.dumps({
        "stations": batch
    }).encode("utf-8")

    for attempt in range(1, 4):

        try:

            request = urllib.request.Request(
                API_URL + "/api/v1/import/stations",
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-import-key": IMPORT_SECRET
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=120
            ) as response:

                result = json.loads(
                    response.read().decode("utf-8")
                )

            if result.get("status") != "ok":
                raise RuntimeError(
                    "Importation non confirmee"
                )

            return result

        except urllib.error.HTTPError as error:

            if error.code in (400, 401, 403, 422):
                raise RuntimeError(
                    f"Import refuse : HTTP {error.code}"
                ) from None

            print(
                f"Tentative {attempt} : HTTP {error.code}",
                flush=True
            )

        except Exception as error:

            print(
                f"Tentative {attempt} : "
                f"{type(error).__name__}",
                flush=True
            )

        if attempt < 3:
            time.sleep(attempt * 10)

    raise RuntimeError(
        "Echec envoi du lot"
    )


def main():

    print("Demarrage importation suisse", flush=True)

    elements = fetch_stations()

    stations = prepare_stations(elements)

    total = len(stations)

    if total == 0:
        raise RuntimeError(
            "Aucune station valide"
        )

    print(
        f"{total} stations a transmettre",
        flush=True
    )

    imported = 0

    for start in range(0, total, BATCH_SIZE):

        batch = stations[
            start:start + BATCH_SIZE
        ]

        result = send_batch(batch)

        imported += len(batch)

        print(
            f"Progression : {imported}/{total} "
            f"- Total PostgreSQL : {result['total']}",
            flush=True
        )

        time.sleep(1)

    print(
        f"Importation terminee : {imported} stations",
        flush=True
    )


if __name__ == "__main__":
    main()
