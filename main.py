from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import os

app = FastAPI()

# Временная "база данных" в памяти
FAKE_DB = {
    "ТПК00001660": {
        "order_id": "ТПК00001660",
        "address": "Подольск",
        "carrier": "Лого-Транс",
        "status": "в пути",
        "remaining_km": 450,
        "eta_date": "07.10.2026"
    }
}

@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Отслеживание заказа</title></head>
    <body style="font-family: Arial; padding: 40px; background: #f0f2f5;">
        <div style="max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px;">
            <h1>📍 Отслеживание заказа</h1>
            <input id="orderInput" placeholder="Введите номер заказа" style="padding:10px; width:70%;"/>
            <button onclick="track()" style="padding:10px;">Найти</button>
            <div id="result" style="margin-top:20px;"></div>
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
                <p><b>Статус:</b> ${data.status}</p>
                <p><b>Осталось:</b> ${data.remaining_km} км</p>
                <p><b>Срок:</b> ${data.eta_date}</p>
            `;
        }
        </script>
    </body>
    </html>
    """

@app.get("/api/track/{order_id}")
async def track_order(order_id: str):
    order = FAKE_DB.get(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return order

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
