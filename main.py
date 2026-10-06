from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import os
import asyncpg

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable must be set")

# Пул соединений с базой (создаём при старте)
pool = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shipments (
                order_id VARCHAR(20) PRIMARY KEY,
                direction VARCHAR(100),
                address TEXT,
                weight DECIMAL,
                ship_date DATE,
                plan_arrival DATE,
                carrier VARCHAR(100),
                vehicle VARCHAR(100),
                driver TEXT,
                gps_link TEXT,
                status VARCHAR(20) DEFAULT 'планируется',
                current_lat DECIMAL(10,6),
                current_lng DECIMAL(10,6),
                last_position_update TIMESTAMP,
                remaining_km INTEGER,
                eta_date DATE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()

# ==== ПОЛЬЗОВАТЕЛЬСКИЙ API ====
@app.get("/api/track/{order_id}")
async def track_order(order_id: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM shipments WHERE order_id = $1", order_id)
    if not row:
        raise HTTPException(404, f"Заказ {order_id} не найден")
    return dict(row)

# ==== АДМИН-API (для будущего) ====
@app.get("/api/admin/shipments")
async def admin_shipments():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM shipments ORDER BY order_id")
    return [dict(r) for r in rows]

# ==== ПОЛЬЗОВАТЕЛЬСКАЯ СТРАНИЦА ====
@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Отслеживание заказа</title>
    <style>
        body { font-family: Arial; padding: 40px; background: #f0f2f5; }
        .card { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; }
        input { padding:10px; width:70%; border:1px solid #ccc; border-radius:4px; }
        button { padding:10px 20px; background:#007bff; color:white; border:none; border-radius:4px; cursor:pointer; }
        #result { margin-top:20px; }
    </style>
    </head>
    <body>
        <div class="card">
            <h1>📍 Отслеживание заказа</h1>
            <input id="orderInput" placeholder="Введите номер заказа"/>
            <button onclick="track()">Найти</button>
            <div id="result"></div>
        </div>
        <script>
        async function track() {
            const order = document.getElementById('orderInput').value.trim();
            const res = await fetch(`/api/track/${order}`);
            const data = await res.json();
            if (!res.ok) {
                document.getElementById('result').innerHTML = `<p style="color:red">${data.detail}</p>`;
                return;
            }
            document.getElementById('result').innerHTML = `
                <h2>📦 ${data.order_id}</h2>
                <p><b>Адрес:</b> ${data.address || '—'}</p>
                <p><b>Перевозчик:</b> ${data.carrier || '—'}</p>
                <p><b>Машина:</b> ${data.vehicle || '—'}</p>
                <p><b>Статус:</b> ${data.status}</p>
            `;
        }
        </script>
    </body>
    </html>
    """
