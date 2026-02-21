import json, os, uuid
from flask import Flask, request, jsonify
import boto3
import psycopg2
import time

app = Flask(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SQS_ENDPOINT = os.getenv("SQS_ENDPOINT")
QUEUE_NAME = os.getenv("QUEUE_NAME", "pagos-queue")
DATABASE_URL = os.getenv("DATABASE_URL")

sqs = boto3.client("sqs", region_name=AWS_REGION, endpoint_url=SQS_ENDPOINT)

def db():
    return psycopg2.connect(DATABASE_URL)

def ensure_tables():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reservas (
          id UUID PRIMARY KEY,
          cliente TEXT NOT NULL,
          monto NUMERIC NOT NULL,
          estado TEXT NOT NULL
        );
        """)
        conn.commit()

def queue_url(max_retries: int = 30, delay_sec: float = 1.0) -> str:
    last_err = None
    for i in range(max_retries):
        try:
            return sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
        except Exception as e:
            last_err = e
            print(f"[RESERVAS] Cola '{QUEUE_NAME}' aún no existe (intento {i+1}/{max_retries}). Reintentando...")
            time.sleep(delay_sec)
    raise last_err

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/reservas")
def crear_reserva():
    payload = request.get_json(force=True)
    cliente = payload.get("cliente", "anon")
    monto = payload.get("monto", 0)

    reserva_id = uuid.uuid4()

    # 1) Guardar reserva
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reservas (id, cliente, monto, estado) VALUES (%s, %s, %s, %s)",
            (str(reserva_id), cliente, monto, "PENDIENTE_PAGO")
        )
        conn.commit()

    # 2) Enviar mensaje a SQS
    msg = {
        "eventType": "ReservaCreada",
        "reservationId": str(reserva_id),
        "monto": float(monto),
        "cliente": cliente,
        "idempotencyKey": str(reserva_id)
    }

    sqs.send_message(
        QueueUrl=queue_url(),
        MessageBody=json.dumps(msg),
        MessageAttributes={
            "eventType": {"StringValue": "ReservaCreada", "DataType": "String"}
        }
    )

    return jsonify({"reservationId": str(reserva_id), "estado": "PENDIENTE_PAGO"}), 202

@app.get("/reservas/<reserva_id>")
def obtener_reserva(reserva_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, cliente, monto, estado FROM reservas WHERE id=%s", (reserva_id,))
        row = cur.fetchone()
        if not row:
            return {"error": "No existe"}, 404

        return {
            "reservationId": str(row[0]),
            "cliente": row[1],
            "monto": float(row[2]),
            "estado": row[3]
        }

if __name__ == "__main__":
    ensure_tables()
    app.run(host="0.0.0.0", port=5000, debug=True)