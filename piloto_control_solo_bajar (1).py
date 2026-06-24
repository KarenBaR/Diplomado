# -*- coding: utf-8 -*-
"""
=====================================================================
 PILOTO / CONTROL - Diseño SOLO BAJAR
=====================================================================
Objetivo: medir cuanto se pierde al NO gestionar bien a un cliente
valioso. En cada banda alta, un 20% al azar se gestiona un nivel por
debajo de lo que recomienda el modelo (CONTROL); el 80% mantiene su
prioridad (PILOTO). La diferencia de conversion = costo de sub-gestionar.

  P1 : 80% PILOTO (gestion P1) | 20% CONTROL -> gestion P2
  P2 : 80% PILOTO (gestion P2) | 20% CONTROL -> gestion P3
  P3 y P4: sin intervencion (fuera del experimento).

USO:
  python piloto_control_solo_bajar.py base.csv
  python piloto_control_solo_bajar.py base.csv --frac 0.20 --seed 7
=====================================================================
"""

import argparse, sys
import numpy as np
import pandas as pd
from scipy import stats

# ============================ CONFIG ================================
INPUT_CSV = "base.csv"
GROUP_COL = "grupo_propension_cartera"
SCORE_COL = "score_propension"
RUC_COL   = "numeroruc"
STRATA_EXTRA = ["canal"]                 # se omite solo si no esta en la base

MAP_PRIORIDAD = {"1. MUY ALTO": "P1", "2. ALTO": "P2", "3. MEDIO": "P3", "4. BAJO": "P4"}

# Bandas que se intervienen: cada una baja a la indicada
BAJAR = {"P1": "P2", "P2": "P3"}         # agrega "P3":"P4" si quieres incluir P3
FRAC_CONTROL = 0.20                       # % que baja un nivel (control)

# Potencia
BASELINE = {"P1": 0.12, "P2": 0.10, "P3": 0.06}   # tasa control esperada (CALIBRAR)
LIFT_RELATIVO, ALPHA, POWER_OBJETIVO = 0.15, 0.05, 0.80

COVARIABLES = [
    "tiempo_vida_empresa", "cant_trabajadores", "ingreso_bruto_total_rrll",
    "deuda_total_max_12m", "cant_empresas_max_12m", "prm_sldtotfintrx12m", SCORE_COL,
]
SENTINEL, SEED = -9.999999999e9, 2026
# ====================================================================


def estratos(df): return [c for c in STRATA_EXTRA if c in df.columns]


def cargar(path):
    df = pd.read_csv(path, dtype={RUC_COL: str})
    print("=" * 60); print("DIAGNOSTICO"); print("=" * 60)
    print(f"Filas: {len(df):,} | Columnas: {df.shape[1]}")
    if any(c not in df.columns for c in [RUC_COL, SCORE_COL, GROUP_COL]):
        print("[ERROR] Faltan columnas criticas (RUC/score/grupo). Ajusta CONFIG."); sys.exit(1)
    est = estratos(df)
    print("Estratificacion: quintil de score" + (f" + {est}" if est else " (solo score)"))
    for c in est:
        df[c] = df[c].astype(str).str.upper().str.strip()
    df["_o"] = df[GROUP_COL].astype(str).str.extract(r"^(\d+)").astype(float)
    df = (df.sort_values(["_o", SCORE_COL], ascending=[True, False])
            .drop_duplicates(RUC_COL, keep="first").drop(columns="_o"))
    df["prioridad"] = df[GROUP_COL].map(MAP_PRIORIDAD)
    df = df[df.prioridad.notna()].copy()
    for c in COVARIABLES:
        if c in df.columns:
            df[c] = df[c].replace(SENTINEL, np.nan)
    print()
    return df.reset_index(drop=True)


def asignar_baja(sub, gestion_baja, frac, rng):
    sub = sub.copy()
    try:
        sub["_q"] = pd.qcut(sub[SCORE_COL], 5, labels=False, duplicates="drop").fillna(-1).astype(int)
    except ValueError:
        sub["_q"] = 0
    keys = estratos(sub) + ["_q"]; by = keys if len(keys) > 1 else keys[0]
    sub["rol_experimento"] = "PILOTO"
    sub["grupo_gestion_asignado"] = sub["prioridad"]
    for _, idx in sub.groupby(by).groups.items():
        idx = np.array(idx); rng.shuffle(idx)
        k = max(1, int(round(len(idx) * frac))) if len(idx) >= 2 else 0
        sel = idx[:k]
        sub.loc[sel, "rol_experimento"] = "CONTROL"
        sub.loc[sel, "grupo_gestion_asignado"] = gestion_baja
    return sub.drop(columns="_q")


def smd(a, b):
    a, b = a.dropna(), b.dropna()
    if len(a) < 2 or len(b) < 2: return np.nan
    sp = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
    return np.nan if sp == 0 else (a.mean() - b.mean()) / sp


def potencia(p, lift, n_ctrl, n_pil, alpha, pobj):
    p2 = p * (1 + lift); za = stats.norm.ppf(1 - alpha / 2)
    se = np.sqrt(p * (1 - p) / max(n_ctrl, 1) + p2 * (1 - p2) / max(n_pil, 1))
    z = abs(p2 - p) / se if se > 0 else 0.0
    power = stats.norm.cdf(z - za) + stats.norm.cdf(-z - za)
    zb = stats.norm.ppf(pobj)
    se0 = np.sqrt(p * (1 - p) * (1 / max(n_ctrl, 1) + 1 / max(n_pil, 1)))
    return power, (za + zb) * se0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archivo", nargs="?", default=INPUT_CSV)
    ap.add_argument("--salida", default=".")
    ap.add_argument("--frac", type=float, default=FRAC_CONTROL)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    df = cargar(args.archivo)
    partes = []
    for banda, baja_a in BAJAR.items():
        sub = df[df.prioridad == banda]
        if len(sub) == 0: continue
        s = asignar_baja(sub, baja_a, args.frac, rng)
        s["experimento"] = f"EXP_{banda}_baja"
        partes.append(s)
    # Bandas que NO se intervienen: se incluyen igual, marcadas como gestion normal
    resto = df[~df.prioridad.isin(BAJAR.keys())].copy()
    if len(resto):
        resto["rol_experimento"] = "SIN_INTERVENCION"
        resto["grupo_gestion_asignado"] = resto["prioridad"]
        resto["experimento"] = "SIN_INTERVENCION"
        partes.append(resto)
    exp = pd.concat(partes, ignore_index=True)

    est = estratos(exp)
    cols = [RUC_COL] + est + [GROUP_COL, "prioridad", "experimento",
            "rol_experimento", "grupo_gestion_asignado", SCORE_COL]
    out_a = f"{args.salida}/piloto_control_asignacion.csv"
    out_b = f"{args.salida}/piloto_control_balance.csv"
    exp[cols].to_csv(out_a, index=False, encoding="utf-8-sig")

    # balance + resumen + potencia
    filas = []
    print("=" * 60); print("RESUMEN + POTENCIA (costo de sub-gestionar)"); print("=" * 60)
    for banda in BAJAR:
        sub = exp[exp.prioridad == banda]
        if len(sub) == 0: continue
        pil = sub[sub.rol_experimento == "PILOTO"]; ctr = sub[sub.rol_experimento == "CONTROL"]
        for c in COVARIABLES:
            if c in exp.columns:
                v = smd(pil[c], ctr[c])
                filas.append({"experimento": f"EXP_{banda}_baja", "covariable": c,
                              "media_piloto": round(pil[c].mean(), 4), "media_control": round(ctr[c].mean(), 4),
                              "SMD": round(v, 4) if pd.notna(v) else np.nan,
                              "balanceado": "SI" if pd.notna(v) and abs(v) < 0.1 else "REVISAR"})
        pb = BASELINE.get(banda, 0.1)
        pw, mde = potencia(pb, LIFT_RELATIVO, len(ctr), len(pil), ALPHA, POWER_OBJETIVO)
        print(f"\n{banda} -> baja a {BAJAR[banda]}:  PILOTO={len(pil):>5}  CONTROL={len(ctr):>5}")
        print(f"   base_control={pb*100:.1f}%  Potencia(+{LIFT_RELATIVO*100:.0f}%): {pw*100:.1f}%"
              f"  {'OK' if pw>=0.8 else '-> acumular periodos'}")
        print(f"   MDE(80%): +{mde*100:.2f} pp ({pb*100:.1f}% -> {(pb+mde)*100:.1f}%)")
    pd.DataFrame(filas).to_csv(out_b, index=False, encoding="utf-8-sig")
    print(f"\nArchivos:\n  {out_a}\n  {out_b}")


if __name__ == "__main__":
    main()
