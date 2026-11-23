
import os
import psycopg
from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="Swiss Fuel Price API",
    description="API suisse de comparaison des prix du carburant",
    version="0.2.0",
)


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


def get_database_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL non configuree"
        )
    return url


@app.get("/api/v1/stations")
def list_stations():
    try:
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
                        longitude DOUBLE PRECISION,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

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

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erreur base de donnees : {str(e)}"
        )
