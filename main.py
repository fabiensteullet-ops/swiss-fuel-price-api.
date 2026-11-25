
import os
import hmac
import logging

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Literal


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("swiss-fuel-price")

app = FastAPI(
    title="Swiss Fuel Price API",
    description="API suisse de comparaison des prix du carburant",
    version="0.5.0",
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


@app.get("/")
def home():
    return {
        "app": "Swiss Fuel Price",
        "version": "0.5.0",
        "status": "online",
        "message": "Bienvenue sur Swiss Fuel Price"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


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

        logger.exception("Erreur PostgreSQL")

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


@app.get("/api/v1/stations/count")
def count_stations():

    try:
        init_db()

        with psycopg.connect(
            get_database_url(),
            connect_timeout=10
        ) as conn:

            with conn.cursor() as cur:

                cur.execute(
                    "SELECT COUNT(*) FROM stations"
                )

                total = cur.fetchone()[0]

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

        with psycopg.connect(
            get_database_url(),
            connect_timeout=10
        ) as conn:

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

                cur.execute(
                    "SELECT COUNT(*) FROM stations"
                )

                total = cur.fetchone()[0]

        imported = len(batch.stations)

        logger.info(
            "Lot traite : %s stations. Total : %s",
            imported,
            total
        )

        return {
            "status": "ok",
            "imported": imported,
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
