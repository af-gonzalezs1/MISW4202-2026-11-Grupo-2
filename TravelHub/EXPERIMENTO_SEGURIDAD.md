# Experimento de Seguridad - TravelHub

Este runbook valida:
- `ASR015`: almacenamiento seguro (RBAC + export cifrado).
- `ASR016`: actualización crítica autorizada y protegida contra tampering/replay.

## 1) Preparación

Desde `TravelHub/`:

```bash
docker compose up -d --build
```

Verifica salud:

```bash
curl -s http://localhost:5001/health
curl -s http://localhost:5002/health
```

## 2) Datos base

Crea una reserva:

```bash
curl -s -X POST http://localhost:5001/reservas \
  -H "Content-Type: application/json" \
  -d '{"cliente":"ana","monto":120.5}'
```

Guarda el `reservationId` retornado (ejemplo: `RID`).

## 3) Tokens por rol (RBAC)

### 3.1 Token ADMIN

```bash
ADMIN_TOKEN=$(curl -s -X POST http://localhost:5001/admin/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin1","role":"ADMIN"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "$ADMIN_TOKEN" | cut -c1-40
```

### 3.2 Token AGENTE (no autorizado)

```bash
AGENTE_TOKEN=$(curl -s -X POST http://localhost:5001/admin/login \
  -H "Content-Type: application/json" \
  -d '{"username":"agente1","role":"AGENTE"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "$AGENTE_TOKEN" | cut -c1-40
```

## 4) Pruebas de RBAC en reportes (PII)

### 4.1 Acceso permitido (ADMIN)

```bash
curl -i -s http://localhost:5001/admin/reportes/reservas \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Esperado: `HTTP/1.1 200`.

### 4.2 Acceso denegado (AGENTE)

```bash
curl -i -s http://localhost:5001/admin/reportes/reservas \
  -H "Authorization: Bearer $AGENTE_TOKEN"
```

Esperado: `HTTP/1.1 403`.

## 5) Export cifrado (datos ilegibles sin llave)

```bash
curl -s http://localhost:5001/admin/reportes/export \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Esperado:
- `HTTP 200`.
- Campo `ciphertext_b64` (no contiene datos legibles de `cliente` o `monto` en claro).

## 6) Anti-tampering en actualización crítica

Se prueba endpoint crítico:
- `PATCH /admin/reservas/<RID>/estado`
- Body: `{"estado":"PAGADA"}`
- Headers obligatorios: `X-Timestamp`, `X-Request-Id`, `X-Signature`.

Define variables:

```bash
RID="<reservationId>"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
REQ_ID=$(cat /proc/sys/kernel/random/uuid)
BODY='{"estado":"PAGADA"}'
PATH_CRIT="/admin/reservas/$RID/estado"
KEY="change-this-signing-secret"
```

Genera firma válida:

```bash
SIG=$(python3 scripts/sign_request.py \
  --method PATCH \
  --path "$PATH_CRIT" \
  --timestamp "$TS" \
  --body "$BODY" \
  --key "$KEY")
echo "$SIG"
```

### 6.1 Request válido (debe persistir)

```bash
curl -i -s -X PATCH "http://localhost:5001$PATH_CRIT" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $TS" \
  -H "X-Request-Id: $REQ_ID" \
  -H "X-Signature: $SIG" \
  -d "$BODY"
```

Esperado: `HTTP/1.1 200`.

### 6.2 Tampering por firma inválida (debe rechazar, sin persistencia)

```bash
curl -i -s -X PATCH "http://localhost:5001$PATH_CRIT" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $TS" \
  -H "X-Request-Id: tamper-1" \
  -H "X-Signature: deadbeef" \
  -d "$BODY"
```

Esperado: `HTTP/1.1 400` con `tampering_detected`.

### 6.3 Replay attack (mismo request_id ya usado)

Repite exactamente la llamada válida de `6.1` con el mismo `REQ_ID`.

Esperado: `HTTP/1.1 400` con razón `replay detected`.

## 7) Evidencia de auditoría

Consulta auditoría:

```bash
curl -s http://localhost:5001/admin/auditoria \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Esperado:
- Eventos `ROLE_CHECK` con `DENY` para intentos no autorizados.
- Eventos `CRITICAL_UPDATE` con `ALLOW` y `DENY`.
- Razones como `invalid signature` o `replay detected`.

## 8) Criterios de aceptación del experimento

Se considera exitoso si:
- Cualquier rol no autorizado recibe `403` en reportes con PII.
- El export se entrega cifrado (`ciphertext_b64`) y no en texto claro.
- Requests alterados/replay en actualización crítica se rechazan con `4xx`.
- No hay persistencia de cambios ante requests inválidos.
- Cada intento queda registrado en `/admin/auditoria`.
