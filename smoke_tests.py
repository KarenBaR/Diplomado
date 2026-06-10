"""
Smoke tests para API CEM.

Uso:
  python smoke_tests.py
  python smoke_tests.py --ruc 20392650793
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from fastapi.testclient import TestClient

from Simulacion2 import app


def run_smoke_tests(ruc: str) -> int:
    client = TestClient(app)
    failures: list[str] = []

    # 1) Health
    health = client.get("/api/v1/health")
    if health.status_code != 200:
        failures.append(f"health status code esperado 200, recibido {health.status_code}")
    else:
        h = health.json()
        if h.get("status") != "healthy":
            failures.append(f"health.status esperado 'healthy', recibido {h.get('status')}")
        if not isinstance(h.get("registros_unicos"), int):
            failures.append("health.registros_unicos debe ser int")

    # 2) Buscar RUC
    buscar = client.post("/api/v1/cem/buscar-ruc", json={"ruc": ruc})
    if buscar.status_code != 200:
        failures.append(f"buscar-ruc status code esperado 200, recibido {buscar.status_code}")
    else:
        b = buscar.json()
        if not isinstance(b.get("encontrado"), bool):
            failures.append("buscar-ruc.encontrado debe ser bool")
        if b.get("ruc") != ruc:
            failures.append(f"buscar-ruc.ruc esperado {ruc}, recibido {b.get('ruc')}")

    # 3) Calcular CEM
    payload_calcular: dict[str, Any] = {
        "ruc": ruc,
        "oferta_solicitada": 50000,
        "plazo_meses": 24,
        "venta_mensual_rp": 15000,
        "gastos_admin_mensual": 2500,
        "gasto_financiero_mensual": 1200,
    }
    calcular = client.post("/api/v1/cem/calcular", json=payload_calcular)
    if calcular.status_code != 200:
        failures.append(f"calcular status code esperado 200, recibido {calcular.status_code}")
    else:
        c = calcular.json()
        required_fields = [
            "exito",
            "alerta",
            "cem_mensual",
            "cuota_estimada_mensual",
            "cem_suficiente",
            "detalle",
        ]
        for field in required_fields:
            if field not in c:
                failures.append(f"calcular sin campo requerido: {field}")

    # 4) Caso borde: RUC invalido (no numerico)
    invalido = client.post("/api/v1/cem/buscar-ruc", json={"ruc": "ABC12345"})
    if invalido.status_code != 400:
        failures.append(
            f"buscar-ruc invalido status code esperado 400, recibido {invalido.status_code}"
        )

    # 5) Caso borde: RUC no existente
    no_encontrado = client.post("/api/v1/cem/buscar-ruc", json={"ruc": "00000000000"})
    if no_encontrado.status_code != 200:
        failures.append(
            f"buscar-ruc no existente status code esperado 200, recibido {no_encontrado.status_code}"
        )
    else:
        n = no_encontrado.json()
        if n.get("encontrado") is not False:
            failures.append("buscar-ruc no existente debe devolver encontrado=false")

    if failures:
        print("\n[FAIL] Smoke tests fallaron:")
        for i, err in enumerate(failures, 1):
            print(f"  {i}. {err}")
        return 1

    print("\n[OK] Smoke tests completados sin errores")
    print(f"  - RUC de prueba: {ruc}")
    print("  - Endpoints validados: health, buscar-ruc, calcular")
    print("  - Casos borde: ruc invalido, ruc no existente")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke tests API CEM")
    parser.add_argument(
        "--ruc",
        default="20392650793",
        help="RUC de prueba existente en base_datos_cem_new.csv",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run_smoke_tests(args.ruc))
