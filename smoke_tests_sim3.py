"""
Smoke tests para Simulacion3 (API CEM v3.1).

Uso:
  python smoke_tests_sim3.py
  python smoke_tests_sim3.py --ruc <numeroruc>
"""

from __future__ import annotations

import argparse
from typing import Any

from fastapi.testclient import TestClient

from API1_TITAN.Simulacion3 import app, cargar_base_datos


def get_default_ruc() -> str:
    """Toma un numeroruc valido desde la base ya normalizada por Simulacion3."""
    df = cargar_base_datos()
    if df.empty or "numeroruc" not in df.columns:
        return ""

    candidatos = df["numeroruc"].astype(str).str.strip()
    candidatos = candidatos[(candidatos != "") & (candidatos.str.lower() != "nan")]
    return candidatos.iloc[0] if not candidatos.empty else ""


def run_smoke_tests(ruc: str | None = None) -> int:
    client = TestClient(app)
    failures: list[str] = []

    ruc_prueba = (ruc or "").strip()

    # 1) Health
    health = client.get("/api/v1/health")
    if health.status_code != 200:
        failures.append(f"health status code esperado 200, recibido {health.status_code}")
    else:
        h = health.json()
        if h.get("status") != "healthy":
            failures.append(f"health.status esperado 'healthy', recibido {h.get('status')}")
        if h.get("fuente") != "base_cem_v3.csv":
            failures.append(f"health.fuente esperado 'base_cem_v3.csv', recibido {h.get('fuente')}")
        if h.get("clave_busqueda") != "numeroruc":
            failures.append(
                f"health.clave_busqueda esperado 'numeroruc', recibido {h.get('clave_busqueda')}"
            )

    # 2) Config periodo (consulta)
    periodo = client.get("/api/v1/config/periodo")
    if periodo.status_code != 200:
        failures.append(f"config/periodo GET esperado 200, recibido {periodo.status_code}")
    else:
        p = periodo.json()
        if "periodo_activo" not in p:
            failures.append("config/periodo GET sin campo periodo_activo")
        if "periodos_disponibles" not in p:
            failures.append("config/periodo GET sin campo periodos_disponibles")

        disponibles = p.get("periodos_disponibles") or []
        periodo_activo = p.get("periodo_activo")
        if not disponibles:
            failures.append("config/periodo GET no devolvio periodos_disponibles")
        elif not periodo_activo:
            set_periodo = client.post("/api/v1/config/periodo", json={"periodo": disponibles[0]})
            if set_periodo.status_code != 200:
                failures.append(
                    f"config/periodo POST esperado 200, recibido {set_periodo.status_code}"
                )

    # 2.1) Obtener RUC de prueba (despues de definir periodo)
    if not ruc_prueba:
        ruc_prueba = get_default_ruc()
    if not ruc_prueba:
        failures.append("No se pudo obtener un numeroruc de prueba desde base_cem_v3.csv")

    # 3) Buscar RUC
    if ruc_prueba:
        buscar = client.post("/api/v1/cem/buscar-ruc", json={"ruc": ruc_prueba})
        if buscar.status_code != 200:
            failures.append(f"buscar-ruc status code esperado 200, recibido {buscar.status_code}")
        else:
            b = buscar.json()
            if b.get("encontrado") is not True:
                failures.append("buscar-ruc esperado encontrado=true para ruc de prueba")
            if b.get("ruc") != ruc_prueba:
                failures.append(f"buscar-ruc.ruc esperado {ruc_prueba}, recibido {b.get('ruc')}")

    # 4) Calcular CEM
    if ruc_prueba:
        payload_calcular: dict[str, Any] = {
            "ruc": ruc_prueba,
            "oferta_solicitada": 50000,
            "plazo_meses": 24,
            "venta_mensual_rp": 15000,
            "gastos_admin_mensual": 2500,
            "tiene_programa_pagos": False,
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

    # 5) Caso borde: RUC vacio
    vacio = client.post("/api/v1/cem/buscar-ruc", json={"ruc": ""})
    if vacio.status_code != 400:
        failures.append(f"buscar-ruc vacio esperado 400, recibido {vacio.status_code}")

    # 6) Caso borde: RUC no existente
    no_encontrado = client.post("/api/v1/cem/buscar-ruc", json={"ruc": "NO_EXISTE_001"})
    if no_encontrado.status_code != 200:
        failures.append(
            f"buscar-ruc no existente status code esperado 200, recibido {no_encontrado.status_code}"
        )
    else:
        n = no_encontrado.json()
        if n.get("encontrado") is not False:
            failures.append("buscar-ruc no existente debe devolver encontrado=false")

    if failures:
        print("\n[FAIL] Smoke tests Simulacion3 fallaron:")
        for i, err in enumerate(failures, 1):
            print(f"  {i}. {err}")
        return 1

    print("\n[OK] Smoke tests Simulacion3 completados sin errores")
    print(f"  - RUC de prueba: {ruc_prueba}")
    print("  - Endpoints validados: health, config/periodo, buscar-ruc, calcular")
    print("  - Casos borde: ruc vacio, ruc no existente")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke tests Simulacion3")
    parser.add_argument("--ruc", default="", help="RUC/numeroruc de prueba (opcional)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run_smoke_tests(args.ruc))
