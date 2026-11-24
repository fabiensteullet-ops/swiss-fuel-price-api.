
import os
import json
import urllib.request
import urllib.parse
import hmac

import psycopg
from fastapi import FastAPI, HTTPException, Header


app = FastAPI(
    title="Swiss Fuel Price API",
    description="API suisse de comparaison des prix du carburant",
    version="0.3.0",
)


def get_database_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL non configuree"
        )
    return url


def init_db():
    with psycopg.connect(get_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stations (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    brand VARCHAR(100),
                    address VARCHAR(250),
                    postal_code VARCHAR(10),
                    city VARCHAR(100),
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION
                )
            """)

            cur.execute("""
                ALTER TABLE stations
                ADD COLUMN IF NOT EXISTS osm_id BIGINT
            """)

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                stations_osm_id_unique
                ON stations(osm_id)
                WHERE osm_id IS NOT NULL
            """)


@app.get("/")
def home():
    return {
        "app": "Swiss Fuel Price",
        "status": "online",
        "message": "Bienvenue sur Swiss Fuel Price"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/stations")
def list_stations():
    try:
        init_db()

        with psycopg.connect(get_database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name, brand, address,
                           postal_code, city, latitude, longitude
                    FROM stations
                    ORDER BY name
                """)

                rows = cur.fetchall()

                items = [
                    {
                        "id": row[0],
                        "name": row[1],
                        "brand": row[2],
                        "address": row[3],
                        "postal_code": row[4],
                        "city": row[5],
                        "latitude": row[6],
                        "longitude": row[7]
                    }
                    for row in rows
                ]

                return {
                    "items": items,
                    "total": len(items),
                    "message": "Stations recuperees depuis PostgreSQL"
                }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erreur base de donnees : {str(e)}"
        )


@app.post("/api/v1/import/stations")
def import_stations(
    x_import_key: str = Header(default="")
):
    secret = os.getenv("IMPORT_SECRET")

    if not secret:
        raise HTTPException(
            status_code=503,
            detail="IMPORT_SECRET non configure"
        )

    if not hmac.compare_digest(x_import_key, secret):
        raise HTTPException(
            status_code=403,
            detail="Cle d'importation incorrecte"
        )

    try:
        init_db()

        query = """
        [out:json][timeout:120];
        area["ISO3166-1"="CH"][admin_level=2]->.ch;
        (
          node["amenity"="fuel"](area.ch);
          way["amenity"="fuel"](area.ch);
          relation["amenity"="fuel"](area.ch);
        );
        out center tags;
        """

        url = "https://overpass-api.de/api/interpreter"

        data = urllib.parse.urlencode(
            {"data": query}
        ).encode()

        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": "SwissFuelPrice/0.3"
            }
        )

        with urllib.request.urlopen(
            request, timeout=180
        ) as response:
            osm_data = json.loads(
                response.read().decode("utf-8")
            )

        elements = osm_data.get("elements", [])
        imported = 0

        with psycopg.connect(get_database_url()) as conn:
            with conn.cursor() as cur:

                for element in elements:
                    tags = element.get("tags", {})

                    name = tags.get("name") or tags.get("brand")
                    if not name:
                        name = "Station sans nom"

                    if element["type"] == "node":
                        latitude = element.get("lat")
                        longitude = element.get("lon")
                    else:
                        center = element.get("center", {})
                        latitude = center.get("lat")
                        longitude = center.get("lon")

                    if latitude is None or longitude is None:
                        continue

                    address_parts = [
                        tags.get("addr:street"),
                        tags.get("addr:housenumber")
                    ]

                    address = " ".join(
                        part for part in address_parts if part
                    ) or None

                    postal_code = tags.get("addr:postcode")
                    city = (
                        tags.get("addr:city")
                        or tags.get("addr:town")
                        or tags.get("addr:village")
                    )

                    osm_id = element["id"]

                    cur.execute("""
                        INSERT INTO stations (
                            osm_id, name, brand, address,
                            postal_code, city, latitude, longitude
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (osm_id)
                        WHERE osm_id IS NOT NULL
                        DO UPDATE SET
                            name = EXCLUDED.name,
                            brand = EXCLUDED.brand,
                            address = EXCLUDED.address,
                            postal_code = EXCLUDED.postal_code,
                            city = EXCLUDED.city,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude
                    """, (
                        osm_id,
                        name,
                        tags.get("brand"),
                        address,
                        postal_code,
                        city,
                        latitude,
                        longitude
                    ))

                    imported += 1

        return {
            "status": "ok",
            "imported": imported,
            "message": "Importation OpenStreetMap terminee"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erreur importation : {str(e)}"
        )
