import base64
import csv
import hashlib
import hmac
import io
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import psycopg2
from cryptography.fernet import Fernet
from flask import Flask, jsonify, request
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    get_jwt,
    jwt_required,
)

app = Flask(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SQS_ENDPOINT = os.getenv("SQS_ENDPOINT")
QUEUE_NAME = os.getenv("QUEUE_NAME", "pagos-queue")
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-me")
REQUEST_SIGNING_KEY = os.getenv("REQUEST_SIGNING_KEY", "dev-signing-key-change-me")
MAX_CLOCK_SKEW_SEC = int(os.getenv("MAX_CLOCK_SKEW_SEC", "300"))

# Must be a Fernet key. If not provided, generate a process-local key for demo use.
EXPORTS_ENCRYPTION_KEY = os.getenv("EXPORTS_ENCRYPTION_KEY") or Fernet.generate_key().decode("utf-8")

app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)
jwt = JWTManager(app)

sqs = boto3.client("sqs", region_name=AWS_REGION, endpoint_url=SQS_ENDPOINT)


def db():
    return psycopg2.connect(DATABASE_URL)


def ensure_tables():
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS reservas (
          id UUID PRIMARY KEY,
          cliente TEXT NOT NULL,
          monto NUMERIC NOT NULL,
          estado TEXT NOT NULL
        );
        """
        )

        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS audit_log (
          id BIGSERIAL PRIMARY KEY,
          ts TIMESTAMP NOT NULL DEFAULT NOW(),
          actor TEXT,
          role TEXT,
          action TEXT NOT NULL,
          resource TEXT,
          outcome TEXT NOT NULL,
          reason TEXT,
          ip TEXT
        );
        """
        )

        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS request_replay_guard (
          request_id TEXT PRIMARY KEY,
          created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """
        )

        conn.commit()


def queue_url(max_retries: int = 30, delay_sec: float = 1.0) -> str:
    last_err = None
    for i in range(max_retries):
        try:
            return sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
        except Exception as e:
            last_err = e
            print(
                f"[RESERVAS] Cola '{QUEUE_NAME}' aún no existe (intento {i+1}/{max_retries}). Reintentando..."
            )
            time.sleep(delay_sec)
    raise last_err


def audit(action: str, resource: str, outcome: str, reason: str = ""):
    identity = None
    role = None
    try:
        claims = get_jwt()
        identity = claims.get("sub")
        role = claims.get("role")
    except Exception:
        pass

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (actor, role, action, resource, outcome, reason, ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (identity, role, action, resource, outcome, reason, request.remote_addr),
        )
        conn.commit()


def roles_required(*allowed_roles):
    def decorator(fn):
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            role = claims.get("role")
            if role not in allowed_roles:
                audit(
                    action="ROLE_CHECK",
                    resource=request.path,
                    outcome="DENY",
                    reason=f"role={role} not in {allowed_roles}",
                )
                return {"error": "forbidden"}, 403
            return fn(*args, **kwargs)

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator


def parse_ts(header_ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(header_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def verify_signed_request() -> tuple[bool, str]:
    ts = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")
    req_id = request.headers.get("X-Request-Id", "")

    if not ts or not sig or not req_id:
        return False, "missing required anti-tampering headers"

    ts_dt = parse_ts(ts)
    if not ts_dt:
        return False, "invalid X-Timestamp format"

    skew = abs((datetime.now(timezone.utc) - ts_dt).total_seconds())
    if skew > MAX_CLOCK_SKEW_SEC:
        return False, "timestamp outside allowed skew"

    body = request.get_data(cache=True, as_text=True)
    canonical = f"{request.method}\n{request.path}\n{ts}\n{body}"
    expected = hmac.new(
        REQUEST_SIGNING_KEY.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sig):
        return False, "invalid signature"

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO request_replay_guard (request_id)
            VALUES (%s)
            ON CONFLICT (request_id) DO NOTHING
            """,
            (req_id,),
        )
        inserted = cur.rowcount == 1
        conn.commit()

    if not inserted:
        return False, "replay detected"

    return True, "ok"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/login")
def admin_login():
    payload = request.get_json(force=True)
    username = payload.get("username", "")
    role = payload.get("role", "")

    if not username or role not in {"ADMIN", "AUDITOR", "AGENTE"}:
        return {"error": "username y role validos son requeridos"}, 400

    token = create_access_token(identity=username, additional_claims={"role": role})
    return {"access_token": token, "role": role}, 200


@app.post("/reservas")
def crear_reserva():
    payload = request.get_json(force=True)
    cliente = payload.get("cliente", "anon")
    monto = payload.get("monto", 0)

    reserva_id = uuid.uuid4()

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reservas (id, cliente, monto, estado) VALUES (%s, %s, %s, %s)",
            (str(reserva_id), cliente, monto, "PENDIENTE_PAGO"),
        )
        conn.commit()

    msg = {
        "eventType": "ReservaCreada",
        "reservationId": str(reserva_id),
        "monto": float(monto),
        "cliente": cliente,
        "idempotencyKey": str(reserva_id),
    }

    sqs.send_message(
        QueueUrl=queue_url(),
        MessageBody=json.dumps(msg),
        MessageAttributes={
            "eventType": {"StringValue": "ReservaCreada", "DataType": "String"}
        },
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
            "estado": row[3],
        }, 200


@app.get("/admin/reportes/reservas")
@roles_required("ADMIN", "AUDITOR")
def reporte_reservas():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, cliente, monto, estado FROM reservas ORDER BY id")
        rows = cur.fetchall()

    audit(action="REPORT_READ", resource="reservas", outcome="ALLOW")
    return {
        "count": len(rows),
        "items": [
            {
                "reservationId": str(r[0]),
                "cliente": r[1],
                "monto": float(r[2]),
                "estado": r[3],
            }
            for r in rows
        ],
    }, 200


@app.get("/admin/reportes/export")
@roles_required("ADMIN", "AUDITOR")
def export_reservas_encrypted():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, cliente, monto, estado FROM reservas ORDER BY id")
        rows = cur.fetchall()

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["reservationId", "cliente", "monto", "estado"])
    for row in rows:
        writer.writerow([str(row[0]), row[1], float(row[2]), row[3]])

    fernet = Fernet(EXPORTS_ENCRYPTION_KEY.encode("utf-8"))
    token = fernet.encrypt(csv_buf.getvalue().encode("utf-8"))

    audit(action="REPORT_EXPORT", resource="reservas", outcome="ALLOW")
    return {
        "algorithm": "Fernet(AES128-CBC+HMAC)",
        "ciphertext_b64": base64.b64encode(token).decode("utf-8"),
    }, 200


@app.patch("/admin/reservas/<reserva_id>/estado")
@roles_required("ADMIN")
def actualizar_estado_critico(reserva_id):
    ok, reason = verify_signed_request()
    if not ok:
        audit(action="CRITICAL_UPDATE", resource=reserva_id, outcome="DENY", reason=reason)
        return {"error": "tampering_detected", "message": "El request llegó tuneado y quedó rechazado.", "reason": reason}, 400

    payload = request.get_json(force=True)
    nuevo_estado = payload.get("estado")
    if nuevo_estado not in {"PENDIENTE_PAGO", "PAGADA", "CANCELADA"}:
        return {"error": "estado invalido"}, 400

    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE reservas SET estado=%s WHERE id=%s", (nuevo_estado, reserva_id))
        if cur.rowcount == 0:
            conn.commit()
            return {"error": "No existe"}, 404
        conn.commit()

    audit(action="CRITICAL_UPDATE", resource=reserva_id, outcome="ALLOW")
    return {"reservationId": reserva_id, "estado": nuevo_estado}, 200


@app.get("/admin/auditoria")
@roles_required("ADMIN", "AUDITOR")
def ver_auditoria():
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, actor, role, action, resource, outcome, reason, ip
            FROM audit_log
            ORDER BY id DESC
            LIMIT 200
            """
        )
        rows = cur.fetchall()

    return {
        "count": len(rows),
        "items": [
            {
                "ts": row[0].isoformat(),
                "actor": row[1],
                "role": row[2],
                "action": row[3],
                "resource": row[4],
                "outcome": row[5],
                "reason": row[6],
                "ip": row[7],
            }
            for row in rows
        ],
    }, 200


if __name__ == "__main__":
    ensure_tables()
    app.run(host="0.0.0.0", port=5000, debug=True)
