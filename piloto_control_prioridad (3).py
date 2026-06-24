# -*- coding: utf-8 -*-
"""
=====================================================================
 PILOTO / CONTROL ESTADISTICO - Diseño final 4 bandas
=====================================================================
Diseño:
  P1 : 80% PILOTO (gestion P1)  | 20% CONTROL -> gestion P2  (baja)
  P2 : 80% PILOTO (gestion P2)  | 20% CONTROL -> gestion P3  (baja)
  P3 : 80% CONTROL (gestion P3) | 20% PROMOVIDO -> P1 / P2   (sube)
  P4 : de los de MAYOR score, 300 PROMOVIDOS -> P1 / P2 (150/150),
       con un CONTROL tomado del mismo pool de alto score (validez
       causal). El resto del P4 queda como referencia, fuera del test.

Contrastes que se miden (cada brazo vs su control de la MISMA banda):
  EXP_P1_baja  : Piloto P1   vs Control (gestion P2)
  EXP_P2_baja  : Piloto P2   vs Control (gestion P3)
  EXP_P3_sube  : Promovido P1 / Promovido P2  vs  Control P3
  EXP_P4_sube  : Promovido P1 / Promovido P2  vs  Control alto-score P4

USO:
  python piloto_control_prioridad.py base.csv
  python piloto_control_prioridad.py base.csv --salida ./out --seed 7
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
STRATA_EXTRA = ["canal"]          # se omiten solas si no estan en la base

MAP_PRIORIDAD = {
    "1. MUY ALTO": "P1", "2. ALTO": "P2", "3. MEDIO": "P3", "4. BAJO": "P4",
}

# --- Palancas del diseño ---
FRAC_BAJA_P1 = 0.20               # % de P1 que baja a gestion P2 (control)
FRAC_BAJA_P2 = 0.20               # % de P2 que baja a gestion P3 (control)
FRAC_SUBE_P3 = 0.20               # % de P3 que sube (se reparte P1/P2)
N_SUBE_P4    = 300                # cantidad fija de P4 que sube (se reparte P1/P2)
P4_POOL_TOP  = 1200               # pool elegible = top score del P4 (de aqui salen
                                  # promovidos + control; debe ser > N_SUBE_P4)

# Tasa de conversion esperada del CONTROL por banda (CALIBRAR con historico real)
BASELINE = {"P1": 0.12, "P2": 0.10, "P3": 0.06, "P4": 0.025}
LIFT_RELATIVO = 0.15
ALPHA, POWER_OBJETIVO = 0.05, 0.80

COVARIABLES = [
    "tiempo_vida_empresa", "cant_trabajadores", "ingreso_bruto_total_rrll",
    "deuda_total_max_12m", "cant_empresas_max_12m", "prm_sldtotfintrx12m", SCORE_COL,
]
SENTINEL, SEED = -9.999999999e9, 2026
# ====================================================================


def estratos(df):
    return [c for c in STRATA_EXTRA if c in df.columns]


def diagnostico(df):
    print("=" * 66); print("DIAGNOSTICO DE LA BASE"); print("=" * 66)
    print(f"Filas: {len(df):,} | Columnas: {df.shape[1]}")
    falt = [c for c in [RUC_COL, SCORE_COL, GROUP_COL] if c not in df.columns]
    if falt:
        print(f"\n[ERROR] Faltan columnas criticas: {falt}"); sys.exit(1)
    est = estratos(df)
    print(f"Estratificacion: quintil de score" + (f" + {est}" if est else " (solo score)"))
    print("Etiquetas de grupo:")
    for e in sorted(map(str, df[GROUP_COL].dropna().unique())):
        print(f"   - {e:<16} -> {MAP_PRIORIDAD.get(e,'(se ignora)')}")
    print()


def cargar(path):
    df = pd.read_csv(path, dtype={RUC_COL: str})
    diagnostico(df)
    for c in estratos(df):
        df[c] = df[c].astype(str).str.upper().str.strip()
    df["_o"] = df[GROUP_COL].astype(str).str.extract(r"^(\d+)").astype(float)
    df = (df.sort_values(["_o", SCORE_COL], ascending=[True, False])
            .drop_duplicates(RUC_COL, keep="first").drop(columns="_o"))
    df["prioridad"] = df[GROUP_COL].map(MAP_PRIORIDAD)
    df = df[df.prioridad.notna()].copy()
    for c in COVARIABLES:
        if c in df.columns:
            df[c] = df[c].replace(SENTINEL, np.nan)
    return df.reset_index(drop=True)


def _quintil(s):
    try:
        return pd.qcut(s, 5, labels=False, duplicates="drop").fillna(-1).astype(int)
    except ValueError:
        return pd.Series(0, index=s.index)


def asignar_baja(sub, frac, gestion_baja, rng):
    """Banda alta: frac -> CONTROL (gestion mas baja), resto PILOTO."""
    sub = sub.copy()
    sub["_q"] = _quintil(sub[SCORE_COL])
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


def asignar_sube_banda(sub, frac_up, rng):
    """P3: frac_up sube (mitad P1 / mitad P2), resto CONTROL."""
    sub = sub.copy()
    sub["_q"] = _quintil(sub[SCORE_COL])
    keys = estratos(sub) + ["_q"]; by = keys if len(keys) > 1 else keys[0]
    sub["rol_experimento"] = "CONTROL"
    sub["grupo_gestion_asignado"] = sub["prioridad"]
    for _, idx in sub.groupby(by).groups.items():
        idx = np.array(idx); rng.shuffle(idx); n = len(idx)
        k = int(round(n * frac_up)); k1 = k // 2; k2 = k - k1
        a, b = idx[:k1], idx[k1:k1 + k2]
        sub.loc[a, ["rol_experimento", "grupo_gestion_asignado"]] = ["PROMOVIDO_P1", "P1"]
        sub.loc[b, ["rol_experimento", "grupo_gestion_asignado"]] = ["PROMOVIDO_P2", "P2"]
    return sub.drop(columns="_q")


def asignar_sube_pool(sub, pool_top, n_prom, rng):
    """P4: pool = top score; dentro del pool aleatoriza n_prom promovidos
       (mitad P1/mitad P2) y el resto del pool como CONTROL alto-score.
       Fuera del pool = REFERENCIA (no entra al experimento)."""
    sub = sub.copy().sort_values(SCORE_COL, ascending=False)
    pool_top = min(pool_top, len(sub))
    pool = sub.iloc[:pool_top].copy()
    resto = sub.iloc[pool_top:].copy()
    pool["rol_experimento"] = "CONTROL"
    pool["grupo_gestion_asignado"] = pool["prioridad"]
    # aleatoriza promovidos dentro del pool, estratificando por canal si existe
    est = estratos(pool)
    pool["_b"] = pool[est[0]] if est else 0
    elig_idx = []
    for _, idx in pool.groupby("_b").groups.items():
        idx = np.array(idx); rng.shuffle(idx); elig_idx.append(idx)
    orden = np.concatenate(elig_idx) if elig_idx else np.array([])
    rng.shuffle(orden)
    n_prom = min(n_prom, len(orden)); k1 = n_prom // 2; k2 = n_prom - k1
    pool.loc[orden[:k1], ["rol_experimento", "grupo_gestion_asignado"]] = ["PROMOVIDO_P1", "P1"]
    pool.loc[orden[k1:k1 + k2], ["rol_experimento", "grupo_gestion_asignado"]] = ["PROMOVIDO_P2", "P2"]
    pool = pool.drop(columns="_b")
    resto["rol_experimento"] = "REFERENCIA"
    resto["grupo_gestion_asignado"] = resto["prioridad"]
    return pd.concat([pool, resto])


def smd(a, b):
    a, b = a.dropna(), b.dropna()
    if len(a) < 2 or len(b) < 2: return np.nan
    sp = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
    return np.nan if sp == 0 else (a.mean() - b.mean()) / sp


def potencia(p_ctrl, lift, n_ctrl, n_trat, alpha, pobj):
    p1 = p_ctrl; p2 = p_ctrl * (1 + lift)
    za = stats.norm.ppf(1 - alpha / 2)
    se = np.sqrt(p1 * (1 - p1) / max(n_ctrl, 1) + p2 * (1 - p2) / max(n_trat, 1))
    z = abs(p2 - p1) / se if se > 0 else 0.0
    power = stats.norm.cdf(z - za) + stats.norm.cdf(-z - za)
    zb = stats.norm.ppf(pobj)
    se0 = np.sqrt(p1 * (1 - p1) * (1 / max(n_ctrl, 1) + 1 / max(n_trat, 1)))
    return power, (za + zb) * se0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archivo", nargs="?", default=INPUT_CSV)
    ap.add_argument("--salida", default=".")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    df = cargar(args.archivo)

    p1 = asignar_baja(df[df.prioridad == "P1"], FRAC_BAJA_P1, "P2", rng)
    p2 = asignar_baja(df[df.prioridad == "P2"], FRAC_BAJA_P2, "P3", rng)
    p3 = asignar_sube_banda(df[df.prioridad == "P3"], FRAC_SUBE_P3, rng)
    p4 = asignar_sube_pool(df[df.prioridad == "P4"], P4_POOL_TOP, N_SUBE_P4, rng)

    p1["experimento"] = "EXP_P1_baja"; p2["experimento"] = "EXP_P2_baja"
    p3["experimento"] = "EXP_P3_sube"
    p4["experimento"] = np.where(p4.rol_experimento == "REFERENCIA", "REFERENCIA", "EXP_P4_sube")

    exp = pd.concat([p1, p2, p3, p4], ignore_index=True)
    est = estratos(exp)
    cols = [RUC_COL] + est + [GROUP_COL, "prioridad", "experimento",
            "rol_experimento", "grupo_gestion_asignado", SCORE_COL]
    out_a = f"{args.salida}/piloto_control_asignacion.csv"
    out_b = f"{args.salida}/piloto_control_balance.csv"
    exp[cols].to_csv(out_a, index=False, encoding="utf-8-sig")

    # ---- contrastes: (nombre, banda, brazo_tratado_df, brazo_control_df) ----
    contrastes = [
        ("P1_baja  Piloto vs Control", "P1", p1[p1.rol_experimento == "PILOTO"], p1[p1.rol_experimento == "CONTROL"]),
        ("P2_baja  Piloto vs Control", "P2", p2[p2.rol_experimento == "PILOTO"], p2[p2.rol_experimento == "CONTROL"]),
        ("P3_sube  ->P1 vs Control",   "P3", p3[p3.rol_experimento == "PROMOVIDO_P1"], p3[p3.rol_experimento == "CONTROL"]),
        ("P3_sube  ->P2 vs Control",   "P3", p3[p3.rol_experimento == "PROMOVIDO_P2"], p3[p3.rol_experimento == "CONTROL"]),
        ("P4_sube  ->P1 vs Control",   "P4", p4[p4.rol_experimento == "PROMOVIDO_P1"], p4[p4.rol_experimento == "CONTROL"]),
        ("P4_sube  ->P2 vs Control",   "P4", p4[p4.rol_experimento == "PROMOVIDO_P2"], p4[p4.rol_experimento == "CONTROL"]),
    ]

    # ---- balance ----
    filas = []
    for nombre, banda, trat, ctrl in contrastes:
        for c in COVARIABLES:
            if c in exp.columns:
                s = smd(trat[c], ctrl[c])
                filas.append({"contraste": nombre, "covariable": c,
                              "media_tratado": round(trat[c].mean(), 4),
                              "media_control": round(ctrl[c].mean(), 4),
                              "SMD": round(s, 4) if pd.notna(s) else np.nan,
                              "balanceado": "SI" if pd.notna(s) and abs(s) < 0.1 else "REVISAR"})
    pd.DataFrame(filas).to_csv(out_b, index=False, encoding="utf-8-sig")

    # ---- resumen ----
    print("=" * 66); print("RESUMEN DE ASIGNACION (RUC unico)"); print("=" * 66)
    print(exp.groupby(["experimento", "rol_experimento", "grupo_gestion_asignado"])
            .size().rename("n").reset_index().to_string(index=False))

    print("\n" + "=" * 66)
    print(f"POTENCIA por contraste (alpha={ALPHA}, lift=+{LIFT_RELATIVO*100:.0f}%)")
    print("=" * 66)
    for nombre, banda, trat, ctrl in contrastes:
        nt, nc = len(trat), len(ctrl); pb = BASELINE.get(banda, 0.1)
        pw, mde = potencia(pb, LIFT_RELATIVO, nc, nt, ALPHA, POWER_OBJETIVO)
        flag = "OK" if pw >= 0.8 else "BAJA -> acumular periodos / mas tamaño"
        print(f"\n{nombre}")
        print(f"   n_tratado={nt:>5}  n_control={nc:>5}  base_control={pb*100:.1f}%")
        print(f"   Potencia(+{LIFT_RELATIVO*100:.0f}%): {pw*100:5.1f}%  [{flag}]")
        print(f"   MDE(80%): +{mde*100:4.2f} pp  ({pb*100:.1f}% -> {(pb+mde)*100:.1f}%)")

    print(f"\nArchivos:\n  {out_a}\n  {out_b}")


if __name__ == "__main__":
    main()
