import os
import hmac
import logging
import json
import math
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from decimal import Decimal
from typing import Literal
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('swiss-fuel-price')
app = FastAPI(title='Swiss Fuel Price API', description='API suisse de comparaison des prix du carburant', version='0.9.0')
FuelType = Literal['SP95', 'SP98', 'DIESEL']
ATTR = '© OpenStreetMap contributors'
COLS = 'id, name, brand, address, postal_code, city, latitude, longitude'

def get_database_url():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise HTTPException(503, 'DATABASE_URL non configuree')
    return url

def get_connection():
    return psycopg.connect(get_database_url(), row_factory=dict_row, connect_timeout=10)

def init_db():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS stations (
            id SERIAL PRIMARY KEY, name VARCHAR(200) NOT NULL,
            brand VARCHAR(100), address VARCHAR(250), postal_code VARCHAR(10),
            city VARCHAR(100), latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
            osm_id BIGINT, osm_type TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        cur.execute('ALTER TABLE stations ADD COLUMN IF NOT EXISTS osm_id BIGINT')
        cur.execute('ALTER TABLE stations ADD COLUMN IF NOT EXISTS osm_type TEXT')
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS stations_osm_type_id_unique ON stations(osm_type, osm_id)')
        cur.execute('DROP INDEX IF EXISTS stations_osm_id_unique')
        cur.execute('''CREATE TABLE IF NOT EXISTS fuel_prices (
            id BIGSERIAL PRIMARY KEY, station_id INTEGER NOT NULL REFERENCES stations(id),
            fuel_type VARCHAR(20) NOT NULL CHECK (fuel_type IN ('SP95','SP98','DIESEL')),
            price NUMERIC(5,3) NOT NULL CHECK (price >= 0.50 AND price <= 5.00),
            currency VARCHAR(3) NOT NULL DEFAULT 'CHF', source VARCHAR(30) NOT NULL DEFAULT 'community',
            reported_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        cur.execute('CREATE INDEX IF NOT EXISTS fuel_prices_station_idx ON fuel_prices(station_id, fuel_type, reported_at DESC, id DESC)')

class StationImport(BaseModel):
    osm_id: int = Field(gt=0)
    osm_type: Literal['node','way','relation']
    name: str = Field(min_length=1,max_length=200)
    brand: str | None = Field(default=None,max_length=100)
    address: str | None = Field(default=None,max_length=250)
    postal_code: str | None = Field(default=None,max_length=10)
    city: str | None = Field(default=None,max_length=100)
    latitude: float = Field(ge=-90,le=90)
    longitude: float = Field(ge=-180,le=180)

class ImportBatch(BaseModel):
    stations: list[StationImport] = Field(min_length=1,max_length=100)

class PriceReport(BaseModel):
    station_id: int = Field(gt=0)
    fuel_type: FuelType
    price: Decimal = Field(ge=Decimal('0.50'),le=Decimal('5.00'),max_digits=5,decimal_places=3)

def fetch(sql, params=()):
    init_db()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

@app.get('/')
def home():
    return {'app':'Swiss Fuel Price','version':'0.9.0','status':'online','message':'Bienvenue sur Swiss Fuel Price'}

@app.get('/health')
def health():
    return {'status':'ok'}

@app.get('/api/v1/database/health')
def database_health():
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        return {'status':'ok','database':'PostgreSQL','connected':True}
    except Exception:
        logger.exception('Erreur PostgreSQL')
        raise HTTPException(503,'Connexion PostgreSQL impossible')

@app.get('/api/v1/stations')
def list_stations():
    items = fetch(f'SELECT {COLS} FROM stations ORDER BY name,id')
    return {'items':items,'total':len(items),'source':'OpenStreetMap','attribution':ATTR,'message':'Stations recuperees depuis PostgreSQL'}

@app.get('/api/v1/stations/count')
def count_stations():
    return {'status':'ok','total':fetch('SELECT COUNT(*) AS total FROM stations')[0]['total']}

DISTANCES = f'''SELECT {COLS}, 6371.0 * 2 * ASIN(SQRT(LEAST(1.0,
    POWER(SIN(RADIANS(latitude - %s)/2),2) + COS(RADIANS(%s))*COS(RADIANS(latitude))*
    POWER(SIN(RADIANS(longitude - %s)/2),2)))) AS distance_km
    FROM stations WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    AND (%s::text IS NULL OR brand ILIKE %s)'''

@app.get('/api/v1/stations/nearby')
def nearby_stations(latitude: float=Query(ge=-90,le=90), longitude: float=Query(ge=-180,le=180), radius_km: float=Query(default=10,ge=1,le=100), limit: int=Query(default=50,ge=1,le=200), brand: str|None=None):
    items = fetch(f'''WITH distances AS ({DISTANCES}) SELECT {COLS},
        ROUND(distance_km::numeric,3)::double precision AS distance_km FROM distances
        WHERE distance_km <= %s ORDER BY distances.distance_km,id LIMIT %s''',
        (latitude,latitude,longitude,brand,brand,radius_km,limit))
    return {'status':'ok','position':{'latitude':latitude,'longitude':longitude},'radius_km':radius_km,'distance_type':'straight_line','brand':brand,'total':len(items),'items':items,'source':'OpenStreetMap','attribution':ATTR}

@app.post('/api/v1/import/stations')
def import_stations(batch: ImportBatch,x_import_key: str=Header(default='')):
    secret = os.getenv('IMPORT_SECRET')
    if not secret: raise HTTPException(503,'IMPORT_SECRET non configure')
    if not hmac.compare_digest(x_import_key.encode(),secret.encode()): raise HTTPException(403,'Cle importation incorrecte')
    init_db()
    with get_connection() as conn, conn.cursor() as cur:
        for s in batch.stations:
            cur.execute('''INSERT INTO stations(osm_id,osm_type,name,brand,address,postal_code,city,latitude,longitude)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(osm_type,osm_id) DO UPDATE SET
                name=EXCLUDED.name,brand=EXCLUDED.brand,address=EXCLUDED.address,postal_code=EXCLUDED.postal_code,
                city=EXCLUDED.city,latitude=EXCLUDED.latitude,longitude=EXCLUDED.longitude''',
                (s.osm_id,s.osm_type,s.name,s.brand,s.address,s.postal_code,s.city,s.latitude,s.longitude))
        cur.execute('SELECT COUNT(*) AS total FROM stations')
        total=cur.fetchone()['total']
    return {'status':'ok','imported':len(batch.stations),'total':total}

def osrm_matrix(latitude, longitude, rows):
    # Two rectangular matrices: origin -> stations and stations -> origin.
    base = os.getenv('OSRM_BASE_URL', 'https://router.project-osrm.org').rstrip('/')
    coords = [(longitude, latitude)] + [(s['longitude'], s['latitude']) for s in rows]
    coordinate_path = ';'.join(f'{lon:.7f},{lat:.7f}' for lon, lat in coords)
    indices = ';'.join(str(i) for i in range(1,len(coords)))
    def table(sources, destinations):
        params = urlencode({'sources':sources,'destinations':destinations,'annotations':'distance,duration'})
        url = f'{base}/table/v1/driving/{coordinate_path}?{params}'
        req = Request(url, headers={'User-Agent':'SwissFuelPrice/0.9.0','Accept':'application/json'})
        with urlopen(req, timeout=8) as response:
            data = json.load(response)
        if data.get('code') != 'Ok':
            raise ValueError('OSRM table error')
        return data['distances'], data['durations']
    # Serialize calls to the public demo service within this request.
    import time
    out_d, out_t = table('0',indices)
    time.sleep(1.1)
    back_d, back_t = table(indices,'0')
    routes = {}
    for i,s in enumerate(rows):
        values = (out_d[0][i],back_d[i][0],out_t[0][i],back_t[i][0])
        if all(isinstance(v,(int,float)) and math.isfinite(v) and v>=0 for v in values):
            routes[s['id']] = (values[0]/1000,values[1]/1000,values[2],values[3])
    return routes

# Static route must precede /stations/{station_id}.
@app.get('/api/v1/stations/cheapest')
def cheapest_stations(latitude: float=Query(ge=-90,le=90), longitude: float=Query(ge=-180,le=180), fuel_type: FuelType='SP95', radius_km: float=Query(default=10,ge=1,le=100), liters: float=Query(default=50,gt=0,le=200), consumption: float=Query(default=6.5,gt=0,le=50), freshness_days: int=Query(default=7,ge=1,le=30), limit: int=Query(default=20,ge=1,le=200), brand: str|None=None, distance_mode: Literal['road','straight_line']='road'):
    rows=fetch(f'''WITH distances AS ({DISTANCES}), latest AS (
        SELECT DISTINCT ON (station_id) station_id,price,currency,source,reported_at
        FROM fuel_prices WHERE fuel_type=%s ORDER BY station_id,reported_at DESC,id DESC)
        SELECT d.*, p.price::double precision AS price,p.currency,p.source AS price_source,p.reported_at
        FROM distances d JOIN latest p ON p.station_id=d.id
        WHERE d.distance_km <= %s AND p.currency='CHF'
        AND p.reported_at >= NOW() - (%s * INTERVAL '1 day') AND p.reported_at <= NOW()''',
        (latitude,latitude,longitude,brand,brand,fuel_type,radius_km,freshness_days))
    eligible_total = len(rows)
    if distance_mode == 'road' and rows:
        # Bound the demo service workload; never silently truncate ranking.
        if len(rows) > 49:
            raise HTTPException(422, 'Trop de stations avec prix: reduire le rayon ou filtrer la marque')
        try:
            routes = osrm_matrix(latitude, longitude, rows)
        except Exception:
            logger.exception('OSRM indisponible')
            raise HTTPException(503, 'OSRM indisponible; reessayer ou utiliser distance_mode=straight_line')
    else:
        routes = {}
    ranked = []
    for s in rows:
        km = s['distance_km']
        s['straight_line_km'] = round(km, 3)
        if distance_mode == 'road':
            route = routes.get(s['id'])
            if route is None:
                continue
            outbound, inbound, seconds_out, seconds_back = route
        else:
            outbound = inbound = km
            seconds_out = seconds_back = None
        trip_km = outbound + inbound
        price = s['price']
        trip_liters = trip_km * consumption / 100
        s.update(distance_km=round(outbound,3),
                 return_distance_km=round(inbound,3),
                 round_trip_km=round(trip_km,3),
                 duration_minutes=round(seconds_out/60,2) if seconds_out is not None else None,
                 return_duration_minutes=round(seconds_back/60,2) if seconds_back is not None else None,
                 round_trip_minutes=round((seconds_out+seconds_back)/60,2) if seconds_out is not None else None,
                 trip_fuel_liters=round(trip_liters,3), fill_cost=round(liters*price,2),
                 trip_cost=round(trip_liters*price,2), total_cost=round((liters+trip_liters)*price,2), verified=False)
        ranked.append(s)
    ranked.sort(key=lambda s:(s['total_cost'],s['distance_km'],s['id']))
    nearest=min(ranked,key=lambda s:(s['distance_km'],s['id'])) if ranked else None
    for rank,s in enumerate(ranked,1):
        s['rank']=rank
        s['savings_vs_nearest']=round(nearest['total_cost']-s['total_cost'],2)
    return {'status':'ok','position':{'latitude':latitude,'longitude':longitude},'fuel_type':fuel_type,'radius_km':radius_km,'radius_type':'straight_line','liters':liters,'consumption':consumption,'freshness_days':freshness_days,'distance_type':distance_mode,'routing_engine':'OSRM' if distance_mode=='road' else None,'estimated':True,'currency':'CHF','total':len(ranked[:limit]),'eligible_total':eligible_total,'routable_total':len(ranked),'excluded_unroutable':eligible_total-len(ranked),'reference_station_id':nearest['id'] if nearest else None,'items':ranked[:limit],'attribution':ATTR}


def require_station(station_id):
    rows=fetch(f'SELECT {COLS} FROM stations WHERE id=%s',(station_id,))
    if not rows: raise HTTPException(404,'Station introuvable')
    return rows[0]

PRICE_COLS='id,fuel_type,price::double precision AS price,currency,source,reported_at'

@app.get('/api/v1/stations/{station_id}')
def station_details(station_id:int):
    station=require_station(station_id)
    prices=fetch('''SELECT DISTINCT ON(fuel_type) fuel_type,price::double precision AS price,currency,source,reported_at FROM fuel_prices WHERE station_id=%s ORDER BY fuel_type,reported_at DESC,id DESC''',(station_id,))
    return {'status':'ok','station':station,'prices':prices,'attribution':ATTR}

@app.post('/api/v1/prices',status_code=201)
def report_price(report:PriceReport):
    station=require_station(report.station_id)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f'''INSERT INTO fuel_prices(station_id,fuel_type,price,currency,source) VALUES (%s,%s,%s,'CHF','community') RETURNING station_id,{PRICE_COLS}''',(report.station_id,report.fuel_type,report.price))
        result=cur.fetchone()
    return {'status':'ok','message':'Prix enregistre','station':station['name'],'verified':False,'data':result}

@app.get('/api/v1/stations/{station_id}/prices')
def station_prices(station_id:int):
    require_station(station_id)
    items=fetch(f'SELECT DISTINCT ON(fuel_type) {PRICE_COLS} FROM fuel_prices WHERE station_id=%s ORDER BY fuel_type,reported_at DESC,id DESC',(station_id,))
    return {'status':'ok','station_id':station_id,'total':len(items),'items':items}

@app.get('/api/v1/stations/{station_id}/prices/history')
def price_history(station_id:int,fuel_type:FuelType|None=None,limit:int=Query(default=100,ge=1,le=1000)):
    require_station(station_id)
    items=fetch(f'SELECT {PRICE_COLS} FROM fuel_prices WHERE station_id=%s AND (%s::text IS NULL OR fuel_type=%s) ORDER BY reported_at DESC,id DESC LIMIT %s',(station_id,fuel_type,fuel_type,limit))
    return {'status':'ok','station_id':station_id,'fuel_type':fuel_type,'total':len(items),'items':items}

@app.get('/api/v1/prices/stats')
def price_stats():
    total=fetch('SELECT COUNT(*) AS total FROM fuel_prices')[0]['total']
    items=fetch('''WITH latest AS (SELECT DISTINCT ON(station_id,fuel_type) station_id,fuel_type,price,reported_at FROM fuel_prices ORDER BY station_id,fuel_type,reported_at DESC,id DESC)
        SELECT fuel_type,COUNT(*) AS stations,MIN(price)::double precision AS min_price,MAX(price)::double precision AS max_price,ROUND(AVG(price),3)::double precision AS average_price FROM latest WHERE reported_at >= NOW()-INTERVAL '7 days' GROUP BY fuel_type ORDER BY fuel_type''')
    return {'status':'ok','total_reports':total,'freshness_days':7,'currency':'CHF','unit':'liter','items':items}
