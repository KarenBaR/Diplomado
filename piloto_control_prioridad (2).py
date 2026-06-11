# -*- coding: utf-8 -*-
"""
=====================================================================
 PILOTO / CONTROL ESTADISTICO - Prioridades comerciales (P1, P2, P3)
 Version reutilizable. 'canal' es OPCIONAL.
=====================================================================
USO:
    python piloto_control_prioridad.py mi_base_nueva.csv
    python piloto_control_prioridad.py mi_base.csv --fc1 0.45 --fc2 0.45
    python piloto_control_prioridad.py mi_base.csv --base 0.18 --lift 0.12

ESTRATIFICACION:
  La aleatorizacion siempre estratifica por QUINTIL de score.
  Ademas estratifica por las columnas de STRATA_EXTRA que existan en la
  base (por defecto 'canal'). Si una columna de STRATA_EXTRA no esta en
  la base (ej. no viene 'canal'), simplemente se omite -> el experimento
  sigue siendo valido, solo estratifica por score.
=====================================================================
"""

import argparse
import sys
import numpy as np
import pandas as pd
from scipy import stats

# ============================ CONFIG ================================
INPUT_CSV   = "base.csv"

GROUP_COL   = "grupo_propension_cartera"
SCORE_COL   = "score_propension"
RUC_COL     = "numeroruc"

# Columnas extra para estratificar/balancear, ADEMAS del quintil de score.
# Las que no existan en la base se omiten solas. Si la base no trae 'canal',
# dejalo igual: el script lo detecta y lo ignora.
STRATA_EXTRA = ["canal"]

MAP_PRIORIDAD = {
    "1. MUY ALTO": "P1",
    "2. ALTO":     "P2",
    "3. MEDIO":    "P3",
}

FRAC_CONTROL_P1 = 0.30
FRAC_CONTROL_P2 = 0.30

BASELINE_CONV   = 0.12
LIFT_RELATIVO   = 0.15
ALPHA           = 0.05
POWER_OBJETIVO  = 0.80

COVARIABLES = [
    "tiempo_vida_empresa", "cant_trabajadores", "ingreso_bruto_total_rrll",
    "deuda_total_max_12m", "cant_empresas_max_12m", "prm_sldtotfintrx12m",
    SCORE_COL,
]
SENTINEL = -9.999999999e9
SEED = 2026
# ====================================================================


def estratos_presentes(df):
    """Columnas de STRATA_EXTRA que existen en la base."""
    return [c for c in STRATA_EXTRA if c in df.columns]


def diagnostico_base(df):
    print("=" * 64)
    print("DIAGNOSTICO DE LA BASE")
    print("=" * 64)
    print(f"Filas: {len(df):,}  |  Columnas: {df.shape[1]}")

    criticas = [RUC_COL, SCORE_COL, GROUP_COL]   # canal NO es critico
    faltan = [c for c in criticas if c not in df.columns]
    if faltan:
        print(f"\n[ERROR] Faltan columnas criticas: {faltan}")
        print("        Ajusta los nombres en CONFIG y vuelve a correr.")
        sys.exit(1)

    est = estratos_presentes(df)
    omit_est = [c for c in STRATA_EXTRA if c not in df.columns]
    print(f"\nEstratificacion: quintil de score" + (f" + {est}" if est else " (solo score)"))
    if omit_est:
        print(f"   (no estan en la base, se omiten: {omit_est})")

    etiquetas = df[GROUP_COL].dropna().unique().tolist()
    print(f"\nEtiquetas en '{GROUP_COL}':")
    for e in sorted(map(str, etiquetas)):
        print(f"   - {e:<16} -> {MAP_PRIORIDAD.get(e, '(sin mapear -> se ignora)')}")
    sin_map = [k for k in MAP_PRIORIDAD if k not in etiquetas]
    if sin_map:
        print(f"\n[AVISO] Etiquetas esperadas que no aparecen: {sin_map}")

    disp = [c for c in COVARIABLES if c in df.columns]
    omit = [c for c in COVARIABLES if c not in df.columns]
    print(f"\nCovariables de balance disponibles: {len(disp)}/{len(COVARIABLES)}")
    if omit:
        print(f"   (se omiten por no existir: {omit})")
    print()


def cargar_y_limpiar(path):
    df = pd.read_csv(path, dtype={RUC_COL: str})
    diagnostico_base(df)
    # Normaliza texto de las columnas de estrato presentes
    for c in estratos_presentes(df):
        df[c] = df[c].astype(str).str.upper().str.strip()
    df["_orden"] = df[GROUP_COL].astype(str).str.extract(r"^(\d+)").astype(float)
    df = (df.sort_values(["_orden", SCORE_COL], ascending=[True, False])
            .drop_duplicates(subset=RUC_COL, keep="first")
            .drop(columns="_orden"))
    df["prioridad"] = df[GROUP_COL].map(MAP_PRIORIDAD)
    df = df[df["prioridad"].notna()].copy()
    for c in COVARIABLES:
        if c in df.columns:
            df[c] = df[c].replace(SENTINEL, np.nan)
    return df.reset_index(drop=True)


def asignar_estratificado(sub, frac_control, rng):
    sub = sub.copy()
    try:
        sub["_q"] = pd.qcut(sub[SCORE_COL], 5, labels=False, duplicates="drop")
    except ValueError:
        sub["_q"] = 0
    sub["_q"] = sub["_q"].fillna(-1).astype(int)
    keys = estratos_presentes(sub) + ["_q"]    # canal si existe, + quintil
    by = keys if len(keys) > 1 else keys[0]    # evita warning de pandas con 1 sola clave
    sub["rol_experimento"] = "PILOTO"
    for _, idx in sub.groupby(by).groups.items():
        idx = np.array(idx)
        rng.shuffle(idx)
        n_ctrl = int(round(len(idx) * frac_control))
        if len(idx) >= 2 and n_ctrl == 0:
            n_ctrl = 1
        sub.loc[idx[:n_ctrl], "rol_experimento"] = "CONTROL"
    return sub.drop(columns="_q")


def smd(a, b):
    a, b = a.dropna(), b.dropna()
    if len(a) < 2 or len(b) < 2:
        return np.nan
    sp = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
    return np.nan if sp == 0 else (a.mean() - b.mean()) / sp


def potencia_dos_proporciones(p_ctrl, lift_rel, n_ctrl, n_pilot, alpha, power_obj):
    p1 = p_ctrl
    p2 = p_ctrl * (1 + lift_rel)
    z_a = stats.norm.ppf(1 - alpha / 2)
    se = np.sqrt(p1 * (1 - p1) / n_ctrl + p2 * (1 - p2) / n_pilot)
    z = abs(p2 - p1) / se if se > 0 else 0.0
    power = stats.norm.cdf(z - z_a) + stats.norm.cdf(-z - z_a)
    z_b = stats.norm.ppf(power_obj)
    se0 = np.sqrt(p1 * (1 - p1) * (1 / n_ctrl + 1 / n_pilot))
    return power, (z_a + z_b) * se0, p1


def main():
    ap = argparse.ArgumentParser(description="Piloto/Control estadistico P1-P2-P3")
    ap.add_argument("archivo", nargs="?", default=INPUT_CSV)
    ap.add_argument("--salida", default=".")
    ap.add_argument("--fc1", type=float, default=FRAC_CONTROL_P1)
    ap.add_argument("--fc2", type=float, default=FRAC_CONTROL_P2)
    ap.add_argument("--base", type=float, default=BASELINE_CONV)
    ap.add_argument("--lift", type=float, default=LIFT_RELATIVO)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    out_asign = f"{args.salida}/piloto_control_asignacion.csv"
    out_balan = f"{args.salida}/piloto_control_balance.csv"

    rng = np.random.default_rng(args.seed)
    df = cargar_y_limpiar(args.archivo)
    est = estratos_presentes(df)    # columnas de estrato realmente presentes

    p1 = asignar_estratificado(df[df.prioridad == "P1"], args.fc1, rng)
    p2 = asignar_estratificado(df[df.prioridad == "P2"], args.fc2, rng)
    p3 = df[df.prioridad == "P3"].copy()
    p3["rol_experimento"] = "REFERENCIA"

    p1["grupo_gestion_asignado"] = np.where(p1.rol_experimento == "CONTROL", "P2", "P1")
    p2["grupo_gestion_asignado"] = np.where(p2.rol_experimento == "CONTROL", "P3", "P2")
    p3["grupo_gestion_asignado"] = "P3"
    p1["experimento"] = "EXP_P1_vs_P2"
    p2["experimento"] = "EXP_P2_vs_P3"
    p3["experimento"] = "REFERENCIA"

    exp = pd.concat([p1, p2, p3], ignore_index=True)
    cols_out = ([RUC_COL] + est + [GROUP_COL, "prioridad", "experimento",
                "rol_experimento", "grupo_gestion_asignado", SCORE_COL])
    exp[cols_out].to_csv(out_asign, index=False, encoding="utf-8-sig")

    filas = []
    for nombre, sub in [("EXP_P1_vs_P2", p1), ("EXP_P2_vs_P3", p2)]:
        pil = sub[sub.rol_experimento == "PILOTO"]
        ctr = sub[sub.rol_experimento == "CONTROL"]
        for c in COVARIABLES:
            if c in sub.columns:
                s = smd(pil[c], ctr[c])
                filas.append({"experimento": nombre, "covariable": c,
                              "media_piloto": round(pil[c].mean(), 4),
                              "media_control": round(ctr[c].mean(), 4),
                              "SMD": round(s, 4) if pd.notna(s) else np.nan,
                              "balanceado(|SMD|<0.1)": "SI" if pd.notna(s) and abs(s) < 0.1 else "REVISAR"})
        # balance de cada estrato categorico presente (ej. canal) via chi2
        for c in est:
            tab = pd.crosstab(sub[c], sub.rol_experimento)
            if tab.shape[0] > 1 and tab.shape[1] > 1:
                _, pval, _, _ = stats.chi2_contingency(tab)
                filas.append({"experimento": nombre, "covariable": f"{c} (chi2 p-value)",
                              "media_piloto": np.nan, "media_control": np.nan, "SMD": round(pval, 4),
                              "balanceado(|SMD|<0.1)": "SI" if pval > 0.05 else "REVISAR"})
    pd.DataFrame(filas).to_csv(out_balan, index=False, encoding="utf-8-sig")

    print("=" * 64)
    print("RESUMEN DE ASIGNACION (a nivel RUC)")
    print("=" * 64)
    print((exp.groupby(["experimento", "rol_experimento", "grupo_gestion_asignado"])
             .size().rename("n_rucs").reset_index()).to_string(index=False))

    print("\n" + "=" * 64)
    print(f"POTENCIA  (alpha={ALPHA}, lift=+{args.lift*100:.0f}%, base control={args.base*100:.1f}%)")
    print("=" * 64)
    for nombre, sub in [("EXP_P1_vs_P2", p1), ("EXP_P2_vs_P3", p2)]:
        n_pil = int((sub.rol_experimento == "PILOTO").sum())
        n_ctr = int((sub.rol_experimento == "CONTROL").sum())
        power, mde_abs, pb = potencia_dos_proporciones(
            args.base, args.lift, n_ctr, n_pil, ALPHA, POWER_OBJETIVO)
        print(f"\n{nombre}:  n_PILOTO={n_pil:>5}  n_CONTROL={n_ctr:>5}")
        print(f"   Potencia (+{args.lift*100:.0f}%): {power*100:5.1f}%  "
              f"{'OK (>=80%)' if power>=0.8 else '<80%: subir --fc o acumular periodos'}")
        print(f"   MDE (80% potencia): +{mde_abs*100:4.2f} pp "
              f"(de {pb*100:.1f}% a {(pb+mde_abs)*100:.1f}% = +{mde_abs/pb*100:4.1f}% rel.)")

    print(f"\nArchivos generados:\n  {out_asign}\n  {out_balan}")


if __name__ == "__main__":
    main()
