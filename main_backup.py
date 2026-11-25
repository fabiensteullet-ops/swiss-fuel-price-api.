import os
import hmac
import logging

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel, Field
from typing import Literal
from decimal import Decimal


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("swiss-fuel-price")

app = FastAPI(
    title="Swiss Fuel Price API",
    description="API suisse de comparaison des prix du carburant",
    version="0.7.0",
)


# DATABASE

def get_database_url():
    url = os.getenv("DATABASE_URL")

    if not url:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL non configuree"
        )

    return url


def get_connection():
    return psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=10
    )


def init_db():
    with get_connection() as conn:
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
                    osm_id BIGINT,
                    osm_type TEXT,
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

            cur.execute("""
                CREATE TABLE IF NOT EXISTS fuel_prices (
                    id BIGSERIAL PRIMARY KEY,
                    station_id INTEGER NOT NULL
                        REFERENCES stations(id),
                    fuel_type VARCHAR(20) NOT NULL
                        CHECK (
                            fuel_type IN (
                                'SP95', 'SP98', 'DIESEL'
                            )
                        ),
                    price NUMERIC(5,3) NOT NULL
                        CHECK (
                            price >= 0.50
                            AND price <= 5.00
                        ),
                    currency VARCHAR(3)
                        NOT NULL DEFAULT 'CHF',
                    source VARCHAR(30)
                        NOT NULL DEFAULT 'community',
                    reported_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                fuel_prices_station_idx
                ON fuel_prices(
                    station_id,
                    fuel_type,
                    reported_at DESC,
                    id DESC
                )
            """)

            logger.info("Database ready")


# MODELS

class StationImport(BaseModel):

    osm_id: int = Field(gt=0)

    osm_type: Literal[
        "node", "way", "relation"
    ]

    name: str = Field(
        min_length=1,
        max_length=200
    )

    brand: str | None = Field(
        default=None,
        max_length=100
    )

    address: str | None = Field(
        default=None,
        max_length=250
    )

    postal_code: str | None = Field(
        default=None,
        max_length=10
    )

    city: str | None = Field(
        default=None,
        max_length=100
    )

    latitude: float = Field(
        ge=-90,
        le=90
    )

    longitude: float = Field(
        ge=-180,
        le=180
    )


class ImportBatch(BaseModel):

    stations: list[StationImport] = Field(
        min_length=1,
        max_length=100
    )


class PriceReport(BaseModel):

    station_id: int = Field(gt=0)

    fuel_type: Literal[
        "SP95",
        "SP98",
        "DIESEL"
    ]

    price: Decimal = Field(
        ge=Decimal("0.50"),
        le=Decimal("5.00"),
        max_digits=5,
        decimal_places=3
    )


# HOME

@app.get("/")
def home():

    return {
        "app": "Swiss Fuel Price",
        "version": "0.7.0",
        "status": "online",
        "message": "Bienvenue sur Swiss Fuel Price"
    }


@app.get("/health")
def health():

    return {"status": "ok"}


# DATABASE HEALTH

@app.get("/api/v1/database/health")
def database_health():

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        return {
            "status": "ok",
            "database": "PostgreSQL",
            "connected": True
        }

    except Exception:

        logger.exception("Erreur PostgreSQL")

        raise HTTPException(
            status_code=503,
            detail="Connexion PostgreSQL impossible"
        )


# LIST STATIONS

@app.get("/api/v1/stations")
def list_stations():

    try:
        init_db()

        with get_connection() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT id, name, brand,
                           address, postal_code,
                           city, latitude, longitude
                    FROM stations
                    ORDER BY name, id
                """)

                items = cur.fetchall()

        return {
            "items": items,
            "total": len(items),
            "source": "OpenStreetMap",
            "attribution":
                "© OpenStreetMap contributors",
            "message":
                "Stations recuperees depuis PostgreSQL"
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception("Erreur lecture stations")

        raise HTTPException(
            status_code=500,
            detail="Erreur lecture PostgreSQL"
        )


# COUNT STATIONS

@app.get("/api/v1/stations/count")
def count_stations():

    try:
        init_db()

        with get_connection() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT COUNT(*) AS total
                    FROM stations
                """)

                total = cur.fetchone()["total"]

        return {
            "status": "ok",
            "total": total
        }

    except Exception:

        logger.exception("Erreur comptage stations")

        raise HTTPException(
            status_code=500,
            detail="Erreur comptage stations"
        )


# GPS NEARBY STATIONS

@app.get("/api/v1/stations/nearby")
def nearby_stations(

    latitude: float = Query(
        ge=-90, le=90
    ),

    longitude: float = Query(
        ge=-180, le=180
    ),

    radius_km: float = Query(
        default=10,
        ge=1,
        le=100
    ),

    limit: int = Query(
        default=50,
        ge=1,
        le=200
    ),

    brand: str | None = None
):

    try:
        init_db()

        with get_connection() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    WITH distances AS (
                        SELECT
                            id,
                            name,
                            brand,
                            address,
                            postal_code,
                            city,
                            latitude,
                            longitude,

                            6371.0 * 2 * ASIN(
                                SQRT(
                                    LEAST(
                                        1.0,
                                        POWER(
                                            SIN(
                                                RADIANS(
                                                    latitude - %s
                                                ) / 2
                                            ), 2
                                        )
                                        +
                                        COS(RADIANS(%s))
                                        *
                                        COS(RADIANS(latitude))
                                        *
                                        POWER(
                                            SIN(
                                                RADIANS(
                                                    longitude - %s
                                                ) / 2
                                            ), 2
                                        )
                                    )
                                )
                            ) AS distance_km

                        FROM stations

                        WHERE latitude IS NOT NULL
                        AND longitude IS NOT NULL

                        AND (
                            %s::text IS NULL
                            OR brand ILIKE %s
                        )
                    )

                    SELECT
                        id, name, brand,
                        address, postal_code,
                        city, latitude, longitude,
                        ROUND(
                            distance_km::numeric, 3
                        )::double precision
                        AS distance_km

                    FROM distances

                    WHERE distance_km <= %s

                    ORDER BY distance_km, id

                    LIMIT %s

                """, (
                    latitude,
                    latitude,
                    longitude,
                    brand,
                    brand,
                    radius_km,
                    limit
                ))

                items = cur.fetchall()

        return {
            "status": "ok",
            "position": {
                "latitude": latitude,
                "longitude": longitude
            },
            "radius_km": radius_km,
            "distance_type": "straight_line",
            "brand": brand,
            "total": len(items),
            "items": items,
            "source": "OpenStreetMap",
            "attribution":
                "© OpenStreetMap contributors"
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception("Erreur recherche GPS")

        raise HTTPException(
            status_code=500,
            detail="Erreur recherche GPS"
        )


# STATION IMPORT

@app.post("/api/v1/import/stations")
def import_stations(

    batch: ImportBatch,

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

        with get_connection() as conn:
            with conn.cursor() as cur:

                for station in batch.stations:

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
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s
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
                        station.osm_id,
                        station.osm_type,
                        station.name,
                        station.brand,
                        station.address,
                        station.postal_code,
                        station.city,
                        station.latitude,
                        station.longitude
                    ))

                cur.execute("""
                    SELECT COUNT(*) AS total
                    FROM stations
                """)

                total = cur.fetchone()["total"]

        return {
            "status": "ok",
            "imported": len(batch.stations),
            "total": total
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception("Erreur importation")

        raise HTTPException(
            status_code=500,
            detail="Erreur importation PostgreSQL"
        )


# STATION DETAILS

@app.get("/api/v1/stations/{station_id}")
def station_details(station_id: int):

    init_db()

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id, name, brand,
                    address, postal_code,
                    city, latitude, longitude

                FROM stations

                WHERE id = %s

            """, (station_id,))

            station = cur.fetchone()

            if not station:

                raise HTTPException(
                    status_code=404,
                    detail="Station introuvable"
                )

            cur.execute("""
                SELECT DISTINCT ON (fuel_type)

                    fuel_type,
                    price::double precision AS price,
                    currency,
                    source,
                    reported_at

                FROM fuel_prices

                WHERE station_id = %s

                ORDER BY
                    fuel_type,
                    reported_at DESC,
                    id DESC

            """, (station_id,))

            prices = cur.fetchall()

    return {
        "status": "ok",
        "station": station,
        "prices": prices,
        "attribution":
            "© OpenStreetMap contributors"
    }


# COMMUNITY PRICE REPORT

@app.post("/api/v1/prices", status_code=201)
def report_price(report: PriceReport):

    try:
        init_db()

        with get_connection() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT id, name
                    FROM stations
                    WHERE id = %s

                """, (report.station_id,))

                station = cur.fetchone()

                if not station:

                    raise HTTPException(
                        status_code=404,
                        detail="Station introuvable"
                    )

                cur.execute("""
                    INSERT INTO fuel_prices (
                        station_id,
                        fuel_type,
                        price,
                        currency,
                        source
                    )

                    VALUES (
                        %s, %s, %s,
                        'CHF', 'community'
                    )

                    RETURNING
                        id,
                        station_id,
                        fuel_type,
                        price::double precision AS price,
                        currency,
                        source,
                        reported_at

                """, (
                    report.station_id,
                    report.fuel_type,
                    report.price
                ))

                result = cur.fetchone()

        return {
            "status": "ok",
            "message": "Prix enregistre",
            "station": station["name"],
            "verified": False,
            "data": result
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception("Erreur enregistrement prix")

        raise HTTPException(
            status_code=500,
            detail="Erreur enregistrement prix"
        )


# LATEST STATION PRICES

@app.get("/api/v1/stations/{station_id}/prices")
def station_prices(station_id: int):

    init_db()

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT id
                FROM stations
                WHERE id = %s

            """, (station_id,))

            if not cur.fetchone():

                raise HTTPException(
                    status_code=404,
                    detail="Station introuvable"
                )

            cur.execute("""
                SELECT DISTINCT ON (fuel_type)

                    id,
                    fuel_type,
                    price::double precision AS price,
                    currency,
                    source,
                    reported_at

                FROM fuel_prices

                WHERE station_id = %s

                ORDER BY
                    fuel_type,
                    reported_at DESC,
                    id DESC

            """, (station_id,))

            items = cur.fetchall()

    return {
        "status": "ok",
        "station_id": station_id,
        "total": len(items),
        "items": items
    }


# PRICE HISTORY

@app.get("/api/v1/stations/{station_id}/prices/history")
def price_history(

    station_id: int,

    fuel_type: Literal[
        "SP95", "SP98", "DIESEL"
    ] | None = None,

    limit: int = Query(
        default=100,
        ge=1,
        le=1000
    )
):

    init_db()

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT id
                FROM stations
                WHERE id = %s

            """, (station_id,))

            if not cur.fetchone():

                raise HTTPException(
                    status_code=404,
                    detail="Station introuvable"
                )

            cur.execute("""
                SELECT
                    id,
                    fuel_type,
                    price::double precision AS price,
                    currency,
                    source,
                    reported_at

                FROM fuel_prices

                WHERE station_id = %s

                AND (
                    %s::text IS NULL
                    OR fuel_type = %s
                )

                ORDER BY
                    reported_at DESC,
                    id DESC

                LIMIT %s

            """, (
                station_id,
                fuel_type,
                fuel_type,
                limit
            ))

            items = cur.fetchall()

    return {
        "status": "ok",
        "station_id": station_id,
        "fuel_type": fuel_type,
        "total": len(items),
        "items": items
    }


# PRICE STATISTICS

@app.get("/api/v1/prices/stats")
def price_stats():

    init_db()

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT COUNT(*) AS total
                FROM fuel_prices
            """)

            total = cur.fetchone()["total"]

            cur.execute("""
                WITH latest AS (

                    SELECT DISTINCT ON (
                        station_id, fuel_type
                    )
                        station_id,
                        fuel_type,
                        price,
                        reported_at

                    FROM fuel_prices

                    ORDER BY
                        station_id,
                        fuel_type,
                        reported_at DESC,
                        id DESC
                )

                SELECT
                    fuel_type,
                    COUNT(*) AS stations,
                    MIN(price)::double precision
                        AS min_price,
                    MAX(price)::double precision
                        AS max_price,
                    ROUND(
                        AVG(price), 3
                    )::double precision
                        AS average_price

                FROM latest

                WHERE reported_at >=
                    NOW() - INTERVAL '7 days'

                GROUP BY fuel_type

                ORDER BY fuel_type
            """)

            items = cur.fetchall()

    return {
        "status": "ok",
        "total_reports": total,
        "freshness_days": 7,
        "currency": "CHF",
        "unit": "liter",
        "items": items
    }
