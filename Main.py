
from fastapi import FastAPI

app = FastAPI(
    title="Swiss Fuel Price API",
    description="API suisse de comparaison des prix du carburant",
    version="0.1.0",
)


@app.get("/")
def home():
    return {
        "app": "Swiss Fuel Price",
        "status": "online",
        "message": "Bienvenue sur l'API Swiss Fuel Price"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/stations")
def list_stations():
    return {
        "items": [],
        "total": 0,
        "message": "La base des stations sera connectée à l'étape suivante."
    }
