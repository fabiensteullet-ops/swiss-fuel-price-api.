
import os
import json
import hmac
import logging
import urllib.request
import urllib.parse

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Header


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("swiss-fuel-price")

app = FastAPI(
    title="Swiss Fuel Price API",
    description="API suisse de comparaison des prix du carburant",
    version="0.4.0",
)

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def get_database_url():
    url = os.getenv("DATABASE_URL")

    if not url:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL non configuree"
        )

    return url


def init_db():
    with psycopg.connect(
        get_database_url(),
        connect_timeout=10
    ) as conn:

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
                    longitude DOUBLE PRECISION,
                    created_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                ALTER TABLE stations
                ADD COLUMN IF NOT EXISTS osm_id BIGINT
            """)

            cur.execute("""
                ALTER TABLE stations
                ADD COLUMN IF NOT EXISTS osm_type TEXT
            """)

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                stations_osm_type_id_unique
                ON stations(osm_type, osm_id)
            """)

            cur.execute("""
                DROP INDEX IF EXISTS
                stations_osm_id_unique
            """)


@app.get("/")
def home():
    return {
        "app": "Swiss Fuel Price",
        "version": "0.4.0",
        "status": "online",
        "message": "Bienvenue sur Swiss Fuel Price"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


@app.get("/api/v1/database/health")
def database_health():

    try:
        with psycopg.connect(
            get_database_url(),
            connect_timeout=10
        ) as conn:

            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        return {
            "status": "ok",
            "database": "PostgreSQL",
            "connected": True
        }

    except Exception:
        logger.exception("Connexion PostgreSQL impossible")

        raise HTTPException(
            status_code=503,
            detail="Connexion PostgreSQL impossible"
        )


@app.get("/api/v1/stations")
def list_stations():

    try:
        init_db()

        with psycopg.connect(
            get_database_url(),
            row_factory=dict_row,
            connect_timeout=10
        ) as conn:

            with conn.cursor() as cur:

                cur.execute("""
                    SELECT
                        id,
                        name,
                        brand,
                        address,
                        postal_code,
                        city,
                        latitude,
                        longitude
                    FROM stations
                    ORDER BY name, id
                """)

                items = cur.fetchall()

        return {
            "items": items,
            "total": len(items),
            "message": "Stations recuperees depuis PostgreSQL"
        }

    except HTTPException:
        raise

    except Exception:
        logger.exception("Erreur lecture stations")

        raise HTTPException(
            status_code=500,
            detail="Erreur de lecture PostgreSQL"
        )


def fetch_osm_stations():

    query = """
    [out:json][timeout:40];

    area["ISO3166-1"="CH"]
        ["admin_level"="2"]->.ch;

    (
        node["amenity"="fuel"](area.ch);
        way["amenity"="fuel"](area.ch);
        relation["amenity"="fuel"](area.ch);
    );

    out center tags;
    """

    data = urllib.parse.urlencode({
        "data": query
    }).encode("utf-8")

    errors = []

    for server in OVERPASS_SERVERS:

        try:
            logger.info(
                "Tentative Overpass : %s",
                server
            )

            request = urllib.request.Request(
                server,
                data=data,
                headers={
                    "User-Agent": "SwissFuelPrice/0.4.0",
                    "Content-Type":
                        "application/x-www-form-urlencoded",
                    "Accept": "application/json"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=55
            ) as response:

                result = json.loads(
                    response.read().decode("utf-8")
                )

            if result.get("remark"):
                raise ValueError(
                    "Reponse Overpass incomplete"
                )

            elements = result.get("elements")

            if not isinstance(elements, list):
                raise ValueError(
                    "Reponse Overpass invalide"
                )

            if not elements:
                raise ValueError(
                    "Aucune station recue"
                )

            logger.info(
                "%s objets recus depuis %s",
                len(elements),
                server
            )

            return elements, server

        except Exception as error:

            logger.warning(
                "Echec Overpass %s : %s",
                server,
                str(error)
            )

            errors.append({
                "server": server,
                "error": type(error).__name__
            })

    raise HTTPException(
        status_code=503,
        detail={
            "message":
                "Tous les serveurs Overpass sont inaccessibles",
            "attempts": errors
        }
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

    if not hmac.compare_digest(
        x_import_key.encode("utf-8"),
        secret.encode("utf-8")
    ):
        raise HTTPException(
            status_code=403,
            detail="Cle importation incorrecte"
        )

    try:
        init_db()

        elements, server = fetch_osm_stations()

        imported = 0
        skipped = 0

        with psycopg.connect(
            get_database_url(),
            connect_timeout=10
        ) as conn:

            with conn.cursor() as cur:

                for element in elements:

                    tags = element.get("tags", {})

                    osm_id = element.get("id")
                    osm_type = element.get("type")

                    name = (
                        tags.get("name")
                        or tags.get("brand")
                        or tags.get("operator")
                        or "Station sans nom"
                    )

                    brand = tags.get("brand")

                    if osm_type == "node":

                        latitude = element.get("lat")
                        longitude = element.get("lon")

                    else:

                        center = element.get("center", {})

                        latitude = center.get("lat")
                        longitude = center.get("lon")

                    if (
                        osm_id is None
                        or osm_type not in (
                            "node", "way", "relation"
                        )
                        or latitude is None
                        or longitude is None
                    ):
                        skipped += 1
                        continue

                    address_parts = [
                        tags.get("addr:street"),
                        tags.get("addr:housenumber")
                    ]

                    address = " ".join(
                        part for part in address_parts
                        if part
                    ) or None

                    postal_code = tags.get(
                        "addr:postcode"
                    )

                    city = (
                        tags.get("addr:city")
                        or tags.get("addr:town")
                        or tags.get("addr:village")
                    )

                    cur.execute("""
                        INSERT INTO stations (
                            osm_id,
                            osm_type,
                            name,
                            brand,
                            address,
                            postal_code,
                            city,
                            latitude,
                            longitude
                        )
                        VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        ON CONFLICT (osm_type, osm_id)
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
                        osm_type,
                        name[:200],
                        brand[:100] if brand else None,
                        address[:250] if address else None,
                        postal_code[:10]
                            if postal_code else None,
                        city[:100] if city else None,
                        latitude,
                        longitude
                    ))

                    imported += 1

                cur.execute(
                    "SELECT COUNT(*) FROM stations"
                )

                total = cur.fetchone()[0]

        logger.info(
            "Importation terminee : %s stations",
            imported
        )

        return {
            "status": "ok",
            "source": "OpenStreetMap",
            "server": server,
            "imported": imported,
            "skipped": skipped,
            "total": total,
            "message": "Importation terminee"
        }

    except HTTPException:
        raise

    except Exception:
        logger.exception("Erreur importation stations")

        raise HTTPException(
            status_code=500,
            detail="Erreur importation. Consulter les logs Render."
        )
