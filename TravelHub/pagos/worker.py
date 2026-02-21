import json, os, time
import boto3
import psycopg2

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
        CREATE TABLE IF NOT EXISTS pagos_idempotencia (
          idempotency_key TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          created_at TIMESTAMP DEFAULT NOW()
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
            print(f"[PAGOS] Cola '{QUEUE_NAME}' aún no existe (intento {i+1}/{max_retries}). Reintentando...")
            time.sleep(delay_sec)
    raise last_err

def cobrar_pasarela_externa(monto: float, cliente: str) -> str:
    # Simulación de cobro
    print(f"[PAGOS] Cobrando a {cliente} por {monto} ...")
    return "OK"

def try_mark_processing(idempotency_key: str) -> bool:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO pagos_idempotencia (idempotency_key, status)
        VALUES (%s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """, (idempotency_key, "PROCESSING"))
        inserted = (cur.rowcount == 1)
        conn.commit()
        return inserted

def mark_done(idempotency_key: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE pagos_idempotencia SET status=%s WHERE idempotency_key=%s",
                    ("DONE", idempotency_key))
        conn.commit()

def main():
    ensure_tables()
    qurl = queue_url()
    print("[PAGOS] Worker escuchando cola...")

    while True:
        resp = sqs.receive_message(
            QueueUrl=qurl,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=10,
            VisibilityTimeout=30
        )

        msgs = resp.get("Messages", [])
        if not msgs:
            continue

        for m in msgs:
            receipt = m["ReceiptHandle"]
            body = json.loads(m["Body"])

            if body.get("eventType") != "ReservaCreada":
                sqs.delete_message(QueueUrl=qurl, ReceiptHandle=receipt)
                continue

            idem = body["idempotencyKey"]
            monto = float(body["monto"])
            cliente = body["cliente"]

            # Idempotencia: evita doble cobro
            if not try_mark_processing(idem):
                print(f"[PAGOS] Ya procesado (idempotente): {idem}")
                sqs.delete_message(QueueUrl=qurl, ReceiptHandle=receipt)
                continue

            try:
                result = cobrar_pasarela_externa(monto, cliente)
                if result != "OK":
                    raise RuntimeError("Fallo pasarela")

                mark_done(idem)
                marcar_reserva_pagada(body["reservationId"])
                print(f"[PAGOS] Pago OK: {idem} -> Reserva PAGADA")
                sqs.delete_message(QueueUrl=qurl, ReceiptHandle=receipt)

            except Exception as e:
                print(f"[PAGOS] Error, reintento luego: {e}")
                time.sleep(1)

def marcar_reserva_pagada(reservation_id: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE reservas SET estado=%s WHERE id=%s", ("PAGADA", reservation_id))
        conn.commit()

if __name__ == "__main__":
    main()