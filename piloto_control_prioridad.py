# -*- coding: utf-8 -*-
"""
=====================================================================
 PILOTO / CONTROL ESTADISTICO - Prioridades comerciales (P1, P2, P3)
=====================================================================
Objetivo:
  Probar causalmente el valor de priorizar a los clientes P1 y P2.
  Diseño: dentro de cada banda se aleatoriza (estratificado) quien
  conserva su prioridad (PILOTO/treatment) y a quien se le "baja"
  un nivel para la gestion (CONTROL):
      - P1: PILOTO gestiona como P1   |  CONTROL gestiona como P2
      - P2: PILOTO gestiona como P2   |  CONTROL gestiona como P3
      - P3: queda como referencia (no se interviene)

  Contrastes que permite medir (intención de tratamiento):
      EXP_P1_vs_P2 : efecto de gestionar como P1 vs P2 (sobre clientes P1)
      EXP_P2_vs_P3 : efecto de gestionar como P2 vs P3 (sobre clientes P2)

  La aleatorizacion es ESTRATIFICADA por canal x quintil de score
  para que PILOTO y CONTROL queden comparables (balance de covariables).

Salidas:
  - piloto_control_asignacion.csv : asignacion a nivel RUC (lista para usar)
  - piloto_control_balance.csv     : reporte de balance (SMD por covariable)
  - resumen impreso: tamaños, potencia y MDE alcanzable.
=====================================================================
"""

import numpy as np
import pandas as pd
from scipy import stats

# ============================ CONFIG ================================
INPUT_CSV   = "5046fa65-0d2f-4cf9-b2f7-c4afdc803b59.csv"
OUT_ASIGN   = "piloto_control_asignacion.csv"
OUT_BALANCE = "piloto_control_balance.csv"

SEED          = 2026          # reproducibilidad
GROUP_COL     = "grupo_propension_cartera"   # columna de grupos a usar
SCORE_COL     = "score_propension"
RUC_COL       = "numeroruc"
CANAL_COL     = "canal"

# Mapeo de las etiquetas del CSV a P1/P2/P3
MAP_PRIORIDAD = {
    "1. MUY ALTO": "P1",
    "2. ALTO":     "P2",
    "3. MEDIO":    "P3",
}

# Fraccion de cada banda que se asigna a CONTROL (se le baja un nivel).
# Es la "palanca" del experimento: mas control = mas potencia, mas costo
# comercial (mas clientes valiosos sub-gestionados).
FRAC_CONTROL_P1 = 0.30
FRAC_CONTROL_P2 = 0.30

# Parametros para el calculo de potencia (two-proportion test).
# BASELINE_CONV = tasa de conversion/desembolso esperada del brazo CONTROL.
# Ajustar con tu tasa historica real por banda.
BASELINE_CONV   = 0.12        # tasa esperada del control (P1 gestionado como P2)
LIFT_RELATIVO   = 0.15        # lift relativo que se quiere detectar (+15%)
ALPHA           = 0.05        # nivel de significancia (2 colas)
POWER_OBJETIVO  = 0.80        # potencia objetivo

# Covariables numericas para el chequeo de balance
COVARIABLES = [
    "tiempo_vida_empresa", "cant_trabajadores", "ingreso_bruto_total_rrll",
    "deuda_total_max_12m", "cant_empresas_max_12m", "prm_sldtotfintrx12m",
    SCORE_COL,
]
SENTINEL = -9.999999999e9      # se trata como NaN
# ====================================================================


def cargar_y_limpiar(path):
    df = pd.read_csv(path, dtype={RUC_COL: str})
    # Normaliza canal (Virtual -> VIRTUAL)
    df[CANAL_COL] = df[CANAL_COL].str.upper().str.strip()
    # Dedup por RUC: conserva la prioridad mas alta (etiqueta menor) y 1 fila
    df["_orden"] = df[GROUP_COL].str.extract(r"^(\d+)").astype(float)
    df = (df.sort_values(["_orden", SCORE_COL], ascending=[True, False])
            .drop_duplicates(subset=RUC_COL, keep="first")
            .drop(columns="_orden"))
    # Mapea a P1/P2/P3 y filtra solo las bandas de interes
    df["prioridad"] = df[GROUP_COL].map(MAP_PRIORIDAD)
    df = df[df["prioridad"].notna()].copy()
    # Limpia centinelas en covariables
    for c in COVARIABLES:
        if c in df.columns:
            df[c] = df[c].replace(SENTINEL, np.nan)
    return df.reset_index(drop=True)


def asignar_estratificado(sub, frac_control, rng):
    """Aleatoriza PILOTO/CONTROL dentro de estratos canal x quintil de score."""
    sub = sub.copy()
    # Quintiles de score dentro de la banda (robusto a pocos valores)
    try:
        sub["_q"] = pd.qcut(sub[SCORE_COL], 5, labels=False, duplicates="drop")
    except ValueError:
        sub["_q"] = 0
    sub["_q"] = sub["_q"].fillna(-1).astype(int)
    sub["rol_experimento"] = "PILOTO"
    for (_, _), idx in sub.groupby([CANAL_COL, "_q"]).groups.items():
        idx = np.array(idx)
        rng.shuffle(idx)
        n_ctrl = int(round(len(idx) * frac_control))
        if len(idx) >= 2 and n_ctrl == 0:   # garantiza al menos 1 en estratos chicos
            n_ctrl = 1
        ctrl = idx[:n_ctrl]
        sub.loc[ctrl, "rol_experimento"] = "CONTROL"
    return sub.drop(columns="_q")


def smd(a, b):
    """Standardized mean difference entre dos series numericas."""
    a, b = a.dropna(), b.dropna()
    if len(a) < 2 or len(b) < 2:
        return np.nan
    sp = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
    return np.nan if sp == 0 else (a.mean() - b.mean()) / sp


def potencia_dos_proporciones(p_ctrl, lift_rel, n_ctrl, n_pilot, alpha):
    """Potencia y MDE para test de 2 proporciones con n desiguales."""
    p1 = p_ctrl
    p2 = p_ctrl * (1 + lift_rel)
    z_a = stats.norm.ppf(1 - alpha / 2)
    se  = np.sqrt(p1 * (1 - p1) / n_ctrl + p2 * (1 - p2) / n_pilot)
    z   = abs(p2 - p1) / se if se > 0 else 0.0
    power = stats.norm.cdf(z - z_a) + stats.norm.cdf(-z - z_a)
    # MDE absoluto detectable al power objetivo
    z_b = stats.norm.ppf(POWER_OBJETIVO)
    se0 = np.sqrt(p1 * (1 - p1) * (1 / n_ctrl + 1 / n_pilot))
    mde_abs = (z_a + z_b) * se0
    return power, mde_abs, p1


def main():
    rng = np.random.default_rng(SEED)
    df = cargar_y_limpiar(INPUT_CSV)

    # ---- Asignacion por banda ----
    partes = []
    p1 = asignar_estratificado(df[df.prioridad == "P1"], FRAC_CONTROL_P1, rng)
    p2 = asignar_estratificado(df[df.prioridad == "P2"], FRAC_CONTROL_P2, rng)
    p3 = df[df.prioridad == "P3"].copy()
    p3["rol_experimento"] = "REFERENCIA"

    # Nivel de gestion asignado (control baja un nivel)
    p1["grupo_gestion_asignado"] = np.where(p1.rol_experimento == "CONTROL", "P2", "P1")
    p2["grupo_gestion_asignado"] = np.where(p2.rol_experimento == "CONTROL", "P3", "P2")
    p3["grupo_gestion_asignado"] = "P3"
    p1["experimento"] = "EXP_P1_vs_P2"
    p2["experimento"] = "EXP_P2_vs_P3"
    p3["experimento"] = "REFERENCIA"

    exp = pd.concat([p1, p2, p3], ignore_index=True)

    cols_out = [RUC_COL, CANAL_COL, GROUP_COL, "prioridad", "experimento",
                "rol_experimento", "grupo_gestion_asignado", SCORE_COL]
    exp[cols_out].to_csv(OUT_ASIGN, index=False, encoding="utf-8-sig")

    # ---- Balance (SMD PILOTO vs CONTROL) ----
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
        # balance de canal (chi2)
        tab = pd.crosstab(sub[CANAL_COL], sub.rol_experimento)
        if tab.shape[0] > 1 and tab.shape[1] > 1:
            chi2, pval, _, _ = stats.chi2_contingency(tab)
            filas.append({"experimento": nombre, "covariable": "canal (chi2 p-value)",
                          "media_piloto": np.nan, "media_control": np.nan,
                          "SMD": round(pval, 4),
                          "balanceado(|SMD|<0.1)": "SI" if pval > 0.05 else "REVISAR"})
    bal = pd.DataFrame(filas)
    bal.to_csv(OUT_BALANCE, index=False, encoding="utf-8-sig")

    # ---- Resumen + Potencia ----
    print("=" * 64)
    print("RESUMEN DE ASIGNACION (a nivel RUC)")
    print("=" * 64)
    resumen = (exp.groupby(["experimento", "rol_experimento", "grupo_gestion_asignado"])
                 .size().rename("n_rucs").reset_index())
    print(resumen.to_string(index=False))

    print("\n" + "=" * 64)
    print("POTENCIA ESTADISTICA  (alpha=%.2f, lift relativo objetivo=+%.0f%%)" %
          (ALPHA, LIFT_RELATIVO * 100))
    print("base esperada control = %.1f%%" % (BASELINE_CONV * 100))
    print("=" * 64)
    for nombre, sub in [("EXP_P1_vs_P2", p1), ("EXP_P2_vs_P3", p2)]:
        n_pil = int((sub.rol_experimento == "PILOTO").sum())
        n_ctr = int((sub.rol_experimento == "CONTROL").sum())
        power, mde_abs, p1b = potencia_dos_proporciones(
            BASELINE_CONV, LIFT_RELATIVO, n_ctr, n_pil, ALPHA)
        print(f"\n{nombre}:  n_PILOTO={n_pil:>5}  n_CONTROL={n_ctr:>5}")
        print(f"   Potencia para detectar +{LIFT_RELATIVO*100:.0f}%: {power*100:5.1f}%"
              f"   {'OK (>=80%)' if power>=0.8 else '<80%: subir FRAC_CONTROL o esperar mas periodos'}")
        print(f"   MDE (al 80% potencia): +{mde_abs*100:4.2f} pp  "
              f"(de {p1b*100:.1f}% a {(p1b+mde_abs)*100:.1f}%  = +{mde_abs/p1b*100:4.1f}% relativo)")

    print("\n" + "=" * 64)
    print("BALANCE PILOTO vs CONTROL (|SMD|<0.1 = comparables)")
    print("=" * 64)
    print(bal.to_string(index=False))
    print(f"\nArchivos: {OUT_ASIGN} | {OUT_BALANCE}")


if __name__ == "__main__":
    main()
