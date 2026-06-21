# -*- coding: utf-8 -*-
"""
Orquestador SALDOS IBK - genera el historico mes a mes en Athena (SageMaker + awswrangler).
- Rango parametrico (PERIODO_DESDE / PERIODO_HASTA en formato YYYYMM).
- Por cada mes: reconstruye el PIVOT (ventana de 12 meses para los lags) y crea
  HM_SALDO_VPC_IBK_FEATURES_<periodo>.
- Al final une todos los meses en HM_SALDO_VPC_IBK_TOTAL.
- Clasificacion de productos corregida: deuda = COLOCACIONES + ACTIVOS RENTABLES,
  ahorros = DEPOSITOS + PASIVOS CON COSTO, + indirectos y mix por categoria.
"""
import time
import awswrangler as wr

# ============================ CONFIG (editar aqui) ============================
DATABASE      = "disc_comercial"
S3_OUTPUT     = "s3://TU-BUCKET/athena-results/"   # <-- ruta de resultados de Athena
PERIODO_DESDE = 202412      # primer mes a generar (YYYYMM)
PERIODO_HASTA = 202605      # ultimo mes a generar  (YYYYMM)
LAG_WINDOW    = 12          # meses hacia atras para los lags (M0..M12)
# =============================================================================


def add_months(yyyymm: int, n: int) -> int:
    y, m = divmod(yyyymm, 100)
    idx = y * 12 + (m - 1) + n
    return (idx // 12) * 100 + (idx % 12) + 1

def month_range(desde: int, hasta: int):
    out, p = [], desde
    while p <= hasta:
        out.append(p)
        p = add_months(p, 1)
    return out

def run_ddl(sql: str, label: str = ""):
    """Ejecuta un DDL/CTAS en Athena y espera a que termine."""
    qid = wr.athena.start_query_execution(sql=sql, database=DATABASE, s3_output=S3_OUTPUT)
    res = wr.athena.wait_query(query_execution_id=qid)
    state = res["Status"]["State"]
    if state != "SUCCEEDED":
        reason = res["Status"].get("StateChangeReason", "")
        raise RuntimeError("[" + label + "] " + state + ": " + str(reason))
    print("  OK [" + label + "] qid=" + qid)
    return qid

def drop_table(name: str):
    run_ddl("DROP TABLE IF EXISTS " + DATABASE + "." + name, "drop " + name)


# ------------------------------ templates SQL -------------------------------
SQL_UNIVERSO = r"""CREATE TABLE disc_comercial.HM_UNIVERSO_MAESTRO_SALDOS
WITH ( 
    format = 'Parquet', 
    parquet_compression = 'SNAPPY'
)
AS (
    -- Todos los RUCs únicos que ALGUNA VEZ tuvieron saldos desde 202501
    WITH CUC_TO_RUC_HISTORICO AS (
        SELECT DISTINCT
            num_ruc_val AS num_ruc
        FROM e_perm_aws.T_VPC_CLIENTE_BANCA_FINAL_HST
        WHERE CAST(YEAR(fecha_dt) * 100 + MONTH(fecha_dt) AS INTEGER) >= {uni_inicio}
          AND CAST(YEAR(fecha_dt) * 100 + MONTH(fecha_dt) AS INTEGER) <= {uni_fin}
          AND num_ruc_val IS NOT NULL
    )
    SELECT DISTINCT num_ruc
    FROM CUC_TO_RUC_HISTORICO
)"""

SQL_PIVOT = r"""CREATE TABLE disc_comercial.HM_SALDO_VPC_IBK_PIVOT
WITH ( 
    format = 'Parquet', 
    parquet_compression = 'SNAPPY', 
    partitioned_by = ARRAY['PERIODO_2']
)
AS (

WITH PARAMS AS (
    SELECT 
        {periodo_objetivo} AS periodo_objetivo,
        {periodo_inicio} AS periodo_inicio  -- 12 meses atrás desde periodo_objetivo
),

-- LISTA DE TODOS LOS PERIODOS
periodos_disponibles AS (
    SELECT DISTINCT periodo_val
    FROM e_perm_aws.t_agg_vpc_saldos_diarios 
    CROSS JOIN PARAMS
    WHERE periodo_val BETWEEN PARAMS.periodo_inicio AND PARAMS.periodo_objetivo
),

-- UNIVERSO BALANCEADO: TODOS los RUC × TODOS los periodos
universo_balanceado AS (
    SELECT 
        m.num_ruc,
        p.periodo_val AS Periodo
    FROM disc_comercial.HM_UNIVERSO_MAESTRO_SALDOS m
    CROSS JOIN periodos_disponibles p
),

SALDOS_DIAS AS (
    SELECT periodo_val, MAX(fecha_saldo_dt) AS fecha_saldo_dt
    FROM e_perm_aws.t_agg_vpc_saldos_diarios
    CROSS JOIN PARAMS
    WHERE periodo_val BETWEEN PARAMS.periodo_inicio AND PARAMS.periodo_objetivo
    GROUP BY periodo_val
),

CUC_TO_RUC AS (
    SELECT 
        PERIODO_VAL,
        num_ruc_val,
        cuc_num
    FROM e_perm_aws.T_VPC_CLIENTE_BANCA_FINAL_HST
    CROSS JOIN PARAMS
    WHERE fecha_dt IN ( 
        SELECT MAX(fecha_dt) AS fecha_dt
        FROM e_perm_aws.T_VPC_CLIENTE_BANCA_FINAL_HST
        CROSS JOIN PARAMS p2
        WHERE CAST(YEAR(fecha_dt) * 100 + MONTH(fecha_dt) AS INTEGER) BETWEEN p2.periodo_inicio AND p2.periodo_objetivo
        GROUP BY CAST(YEAR(fecha_dt) * 100 + MONTH(fecha_dt) AS INTEGER)
    )
),

base_saldos_raw AS (
    SELECT
        aa.periodo_val   AS Periodo,
        b.num_ruc_val    AS num_ruc,
        MAX(CASE WHEN aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN 1 ELSE 0 END) AS flg_colocaciones,
        MAX(CASE WHEN aa.tipo_prod_dsc IN ('DEPÓSITOS','PASIVOS CON COSTO')    THEN 1 ELSE 0 END) AS flg_pasivo,
        MAX(CASE WHEN aa.tipo_prod_dsc = 'CRÉDITOS INDIRECTOS'                 THEN 1 ELSE 0 END) AS flg_indirecto,
        SUM(CASE WHEN aa.tipo_prod_dsc IN ('DEPÓSITOS','PASIVOS CON COSTO')    THEN saldo_actual_sol_mto    ELSE 0 END) AS saldo_punta_pasivo,
        SUM(CASE WHEN aa.tipo_prod_dsc IN ('DEPÓSITOS','PASIVOS CON COSTO')    THEN saldo_promedio_sol_mto  ELSE 0 END) AS saldo_promedio_pasivo,
        SUM(CASE WHEN aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN saldo_promedio_sol_mto  ELSE 0 END) AS saldo_promedio_colocaciones,
        SUM(CASE WHEN aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN saldo_actual_sol_mto    ELSE 0 END) AS saldo_punta_colocaciones,
        SUM(CASE WHEN aa.tipo_prod_dsc = 'CRÉDITOS INDIRECTOS'                 THEN saldo_actual_sol_mto    ELSE 0 END) AS saldo_indirecto,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc LIKE 'PAGARÉ%'                            THEN 1 ELSE 0 END) AS flg_pagare,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc = 'LEASING'                               THEN 1 ELSE 0 END) AS flg_leasing,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc = 'FACTORING'                             THEN 1 ELSE 0 END) AS flg_factoring,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc = 'COMEX'                                 THEN 1 ELSE 0 END) AS flg_comex,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc IN ('AVANCES / SOBREGIROS','DESCUENTOS')  THEN 1 ELSE 0 END) AS flg_credito_corto_plazo,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc = 'PAGARÉ REACTIVA'                       THEN 1 ELSE 0 END) AS flg_reactiva,
        MAX(CASE WHEN aa.categoria_prod_vpc_dsc IN ('CARTAS FIANZA','CARTAS DE CRÉDITO DE IMPORTACIÓN') THEN 1 ELSE 0 END) AS flg_carta,
        SUM(CASE WHEN aa.categoria_prod_vpc_dsc LIKE 'PAGARÉ%' THEN saldo_actual_sol_mto ELSE 0 END) AS saldo_pagares,
        SUM(CASE WHEN aa.categoria_prod_vpc_dsc = 'LEASING'    THEN saldo_actual_sol_mto ELSE 0 END) AS saldo_leasing,
        SUM(CASE WHEN aa.categoria_prod_vpc_dsc = 'FACTORING'  THEN saldo_actual_sol_mto ELSE 0 END) AS saldo_factoring,
        SUM(CASE WHEN aa.categoria_prod_vpc_dsc = 'COMEX'      THEN saldo_actual_sol_mto ELSE 0 END) AS saldo_comex,
        COUNT(DISTINCT CASE WHEN aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN aa.categoria_prod_vpc_dsc END) AS nro_productos_financiamiento,
        COUNT(DISTINCT aa.tipo_prod_dsc) AS nro_tipo_producto,
        COUNT(DISTINCT aa.moneda_val)    AS nro_monedas,
        MAX(CASE WHEN aa.situacion_cartera_cd = '01' AND aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN 1 ELSE 0 END) AS flg_vigente,
        MAX(CASE WHEN aa.situacion_cartera_cd = '05' AND aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN 1 ELSE 0 END) AS flg_vencido,
        MAX(CASE WHEN aa.situacion_cartera_cd = '08' AND aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN 1 ELSE 0 END) AS flg_castigado,
        SUM(CASE WHEN aa.situacion_cartera_cd IN ('05','08') AND aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN saldo_actual_sol_mto ELSE 0 END) AS saldo_deteriorado,
        SUM(CASE WHEN aa.moneda_val = 'USD' AND aa.tipo_prod_dsc IN ('DEPÓSITOS','PASIVOS CON COSTO')    THEN saldo_actual_us_mto ELSE 0 END) AS saldo_pasivo_usd,
        SUM(CASE WHEN aa.moneda_val = 'USD' AND aa.tipo_prod_dsc IN ('COLOCACIONES','ACTIVOS RENTABLES') THEN saldo_actual_us_mto ELSE 0 END) AS saldo_colocaciones_usd
    FROM e_perm_aws.t_agg_vpc_saldos_diarios aa
    INNER JOIN SALDOS_DIAS bb ON aa.periodo_val = bb.periodo_val AND aa.fecha_saldo_dt = bb.fecha_saldo_dt
    INNER JOIN CUC_TO_RUC b ON CAST(aa.codigo_unico_cliente_cd AS VARCHAR) = CAST(b.cuc_num AS VARCHAR) AND CAST(aa.periodo_val AS VARCHAR) = CAST(b.periodo_val AS VARCHAR)
    CROSS JOIN PARAMS
    WHERE aa.periodo_val BETWEEN PARAMS.periodo_inicio AND PARAMS.periodo_objetivo
    GROUP BY aa.periodo_val, b.num_ruc_val
),

base_saldos AS (
    SELECT
        ub.Periodo, ub.num_ruc,
        COALESCE(bs.flg_pasivo, 0) AS flg_pasivo,
        COALESCE(bs.flg_colocaciones, 0) AS flg_colocaciones,
        COALESCE(bs.saldo_punta_pasivo, 0) AS saldo_punta_pasivo,
        COALESCE(bs.saldo_promedio_pasivo, 0) AS saldo_promedio_pasivo,
        COALESCE(bs.saldo_promedio_colocaciones, 0) AS saldo_promedio_colocaciones,
        COALESCE(bs.saldo_punta_colocaciones, 0) AS saldo_punta_colocaciones,
        COALESCE(bs.nro_tipo_producto, 0) AS nro_tipo_producto,
        COALESCE(bs.nro_monedas, 0) AS nro_monedas,
        COALESCE(bs.flg_vigente, 0) AS flg_vigente,
        COALESCE(bs.flg_vencido, 0) AS flg_vencido,
        COALESCE(bs.flg_castigado, 0) AS flg_castigado,
        COALESCE(bs.saldo_deteriorado, 0) AS saldo_deteriorado,
        COALESCE(bs.saldo_pasivo_usd, 0) AS saldo_pasivo_usd,
        COALESCE(bs.saldo_colocaciones_usd, 0) AS saldo_colocaciones_usd,
        COALESCE(bs.flg_indirecto, 0) AS flg_indirecto,
        COALESCE(bs.saldo_indirecto, 0) AS saldo_indirecto,
        COALESCE(bs.flg_pagare, 0) AS flg_pagare,
        COALESCE(bs.flg_leasing, 0) AS flg_leasing,
        COALESCE(bs.flg_factoring, 0) AS flg_factoring,
        COALESCE(bs.flg_comex, 0) AS flg_comex,
        COALESCE(bs.flg_credito_corto_plazo, 0) AS flg_credito_corto_plazo,
        COALESCE(bs.flg_reactiva, 0) AS flg_reactiva,
        COALESCE(bs.flg_carta, 0) AS flg_carta,
        COALESCE(bs.saldo_pagares, 0) AS saldo_pagares,
        COALESCE(bs.saldo_leasing, 0) AS saldo_leasing,
        COALESCE(bs.saldo_factoring, 0) AS saldo_factoring,
        COALESCE(bs.saldo_comex, 0) AS saldo_comex,
        COALESCE(bs.nro_productos_financiamiento, 0) AS nro_productos_financiamiento
    FROM universo_balanceado ub
    LEFT JOIN base_saldos_raw bs ON ub.num_ruc = bs.num_ruc AND ub.Periodo = bs.Periodo
),

saldos_con_lags AS (
    SELECT
        Periodo,
        num_ruc,
        
        -- ==================== VARIABLES ACTUALES (M0) ====================
        flg_pasivo,
        flg_colocaciones,
        saldo_punta_pasivo,
        saldo_promedio_pasivo,
        saldo_promedio_colocaciones,
        saldo_punta_colocaciones,
        nro_tipo_producto,
        nro_monedas,
        flg_vigente,
        flg_vencido,
        flg_castigado,
        saldo_deteriorado,
        saldo_pasivo_usd,
        saldo_colocaciones_usd,
        flg_indirecto,
        saldo_indirecto,
        flg_pagare,
        flg_leasing,
        flg_factoring,
        flg_comex,
        flg_credito_corto_plazo,
        flg_reactiva,
        flg_carta,
        saldo_pagares,
        saldo_leasing,
        saldo_factoring,
        saldo_comex,
        nro_productos_financiamiento,
        
        -- ==================== LAGS M1-M12: FLG_PASIVO ====================
        LAG(flg_pasivo,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_flg_pasivo,
        LAG(flg_pasivo,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_flg_pasivo,
        LAG(flg_pasivo,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_flg_pasivo,
        LAG(flg_pasivo,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_flg_pasivo,
        LAG(flg_pasivo,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_flg_pasivo,
        LAG(flg_pasivo,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_flg_pasivo,
        LAG(flg_pasivo,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_flg_pasivo,
        LAG(flg_pasivo,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_flg_pasivo,
        LAG(flg_pasivo,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_flg_pasivo,
        LAG(flg_pasivo, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_flg_pasivo,
        LAG(flg_pasivo, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_flg_pasivo,
        LAG(flg_pasivo, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_flg_pasivo,
        
        -- ==================== LAGS M1-M12: FLG_COLOCACIONES ====================
        LAG(flg_colocaciones,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_flg_colocaciones,
        LAG(flg_colocaciones,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_flg_colocaciones,
        LAG(flg_colocaciones,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_flg_colocaciones,
        LAG(flg_colocaciones,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_flg_colocaciones,
        LAG(flg_colocaciones,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_flg_colocaciones,
        LAG(flg_colocaciones,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_flg_colocaciones,
        LAG(flg_colocaciones,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_flg_colocaciones,
        LAG(flg_colocaciones,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_flg_colocaciones,
        LAG(flg_colocaciones,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_flg_colocaciones,
        LAG(flg_colocaciones, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_flg_colocaciones,
        LAG(flg_colocaciones, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_flg_colocaciones,
        LAG(flg_colocaciones, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_flg_colocaciones,
        
        -- ==================== LAGS M1-M12: SALDO_PUNTA_PASIVO ====================
        LAG(saldo_punta_pasivo,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_saldo_punta_pasivo,
        LAG(saldo_punta_pasivo, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_saldo_punta_pasivo,
        
        -- ==================== LAGS M1-M12: SALDO_PROMEDIO_PASIVO ====================
        LAG(saldo_promedio_pasivo,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_saldo_promedio_pasivo,
        LAG(saldo_promedio_pasivo, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_saldo_promedio_pasivo,
        
        -- ==================== LAGS M1-M12: SALDO_PROMEDIO_COLOCACIONES ====================
        LAG(saldo_promedio_colocaciones,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_saldo_promedio_colocaciones,
        LAG(saldo_promedio_colocaciones, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_saldo_promedio_colocaciones,
        
        -- ==================== LAGS M1-M12: SALDO_PUNTA_COLOCACIONES ====================
        LAG(saldo_punta_colocaciones,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_saldo_punta_colocaciones,
        LAG(saldo_punta_colocaciones, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_saldo_punta_colocaciones,
        
        -- ==================== LAGS M1-M12: NRO_TIPO_PRODUCTO ====================
        LAG(nro_tipo_producto,  1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_nro_tipo_producto,
        LAG(nro_tipo_producto,  2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_nro_tipo_producto,
        LAG(nro_tipo_producto,  3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_nro_tipo_producto,
        LAG(nro_tipo_producto,  4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_nro_tipo_producto,
        LAG(nro_tipo_producto,  5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_nro_tipo_producto,
        LAG(nro_tipo_producto,  6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_nro_tipo_producto,
        LAG(nro_tipo_producto,  7) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m7_nro_tipo_producto,
        LAG(nro_tipo_producto,  8) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m8_nro_tipo_producto,
        LAG(nro_tipo_producto,  9) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m9_nro_tipo_producto,
        LAG(nro_tipo_producto, 10) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m10_nro_tipo_producto,
        LAG(nro_tipo_producto, 11) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m11_nro_tipo_producto,
        LAG(nro_tipo_producto, 12) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m12_nro_tipo_producto,
        
        -- ==================== LAGS CALIDAD CARTERA (M1, M3, M6) ====================
        LAG(flg_vigente,   1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_flg_vigente,
        LAG(flg_vigente,   3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_flg_vigente,
        LAG(flg_vigente,   6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_flg_vigente,
        LAG(flg_vencido,   1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_flg_vencido,
        LAG(flg_vencido,   2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_flg_vencido,
        LAG(flg_vencido,   3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_flg_vencido,
        LAG(flg_vencido,   4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_flg_vencido,
        LAG(flg_vencido,   5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_flg_vencido,
        LAG(flg_vencido,   6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_flg_vencido,
        LAG(flg_castigado, 1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_flg_castigado,
        LAG(flg_castigado, 2) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m2_flg_castigado,
        LAG(flg_castigado, 3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_flg_castigado,
        LAG(flg_castigado, 4) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m4_flg_castigado,
        LAG(flg_castigado, 5) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m5_flg_castigado,
        LAG(flg_castigado, 6) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m6_flg_castigado,
        LAG(saldo_deteriorado, 1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_deteriorado,
        LAG(saldo_deteriorado, 3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_deteriorado,
        
        -- ==================== LAGS DOLARIZACIÓN (M1, M3) ====================
        LAG(saldo_pasivo_usd,       1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_pasivo_usd,
        LAG(saldo_pasivo_usd,       3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_pasivo_usd,
        LAG(saldo_colocaciones_usd, 1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_saldo_colocaciones_usd,
        LAG(saldo_colocaciones_usd, 3) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m3_saldo_colocaciones_usd,
        LAG(nro_monedas,            1) OVER (PARTITION BY num_ruc ORDER BY Periodo) AS m1_nro_monedas
        
    FROM base_saldos
)

SELECT 
    *,
    CAST(Periodo AS VARCHAR) AS PERIODO_2
FROM saldos_con_lags
CROSS JOIN PARAMS
WHERE Periodo = PARAMS.periodo_objetivo
)"""

SQL_FEATURES = r"""CREATE TABLE disc_comercial.HM_SALDO_VPC_IBK_FEATURES_{periodo_objetivo}
WITH ( 
    format = 'Parquet', 
    parquet_compression = 'SNAPPY', 
    partitioned_by = ARRAY['PERIODO_2']
)
AS (

WITH base_pivot AS (
    SELECT *
    FROM disc_comercial.HM_SALDO_VPC_IBK_PIVOT
),

features_adicionales AS (
    SELECT
        bp.Periodo,
        bp.num_ruc,
        
        -- ==================== VARIABLES BASE ====================
        COALESCE(bp.flg_pasivo, 0)                  AS flg_pasivo,
        COALESCE(bp.flg_colocaciones, 0)             AS flg_colocaciones,
        COALESCE(bp.saldo_punta_pasivo, 0)           AS saldo_punta_pasivo,
        COALESCE(bp.saldo_promedio_pasivo, 0)        AS saldo_promedio_pasivo,
        COALESCE(bp.saldo_promedio_colocaciones, 0)  AS saldo_promedio_colocaciones,
        COALESCE(bp.saldo_punta_colocaciones, 0)     AS saldo_punta_colocaciones,
        COALESCE(bp.nro_tipo_producto, 0)            AS nro_tipo_producto,
        COALESCE(bp.nro_monedas, 0)                  AS nro_monedas,

        COALESCE(bp.flg_indirecto, 0) AS flg_indirecto,
        COALESCE(bp.saldo_indirecto, 0) AS saldo_indirecto,
        COALESCE(bp.flg_pagare, 0) AS flg_pagare,
        COALESCE(bp.flg_leasing, 0) AS flg_leasing,
        COALESCE(bp.flg_factoring, 0) AS flg_factoring,
        COALESCE(bp.flg_comex, 0) AS flg_comex,
        COALESCE(bp.flg_credito_corto_plazo, 0) AS flg_credito_corto_plazo,
        COALESCE(bp.flg_reactiva, 0) AS flg_reactiva,
        COALESCE(bp.flg_carta, 0) AS flg_carta,
        COALESCE(bp.saldo_pagares, 0) AS saldo_pagares,
        COALESCE(bp.saldo_leasing, 0) AS saldo_leasing,
        COALESCE(bp.saldo_factoring, 0) AS saldo_factoring,
        COALESCE(bp.saldo_comex, 0) AS saldo_comex,
        COALESCE(bp.nro_productos_financiamiento, 0) AS nro_productos_financiamiento,
        
        -- LAGS PASIVO
        COALESCE(bp.m1_flg_pasivo,  0) AS m1_flg_pasivo,
        COALESCE(bp.m2_flg_pasivo,  0) AS m2_flg_pasivo,
        COALESCE(bp.m3_flg_pasivo,  0) AS m3_flg_pasivo,
        COALESCE(bp.m4_flg_pasivo,  0) AS m4_flg_pasivo,
        COALESCE(bp.m5_flg_pasivo,  0) AS m5_flg_pasivo,
        COALESCE(bp.m6_flg_pasivo,  0) AS m6_flg_pasivo,
        COALESCE(bp.m7_flg_pasivo,  0) AS m7_flg_pasivo,
        COALESCE(bp.m8_flg_pasivo,  0) AS m8_flg_pasivo,
        COALESCE(bp.m9_flg_pasivo,  0) AS m9_flg_pasivo,
        COALESCE(bp.m10_flg_pasivo, 0) AS m10_flg_pasivo,
        COALESCE(bp.m11_flg_pasivo, 0) AS m11_flg_pasivo,
        COALESCE(bp.m12_flg_pasivo, 0) AS m12_flg_pasivo,
        
        -- LAGS COLOCACIONES
        COALESCE(bp.m1_flg_colocaciones,  0) AS m1_flg_colocaciones,
        COALESCE(bp.m2_flg_colocaciones,  0) AS m2_flg_colocaciones,
        COALESCE(bp.m3_flg_colocaciones,  0) AS m3_flg_colocaciones,
        COALESCE(bp.m4_flg_colocaciones,  0) AS m4_flg_colocaciones,
        COALESCE(bp.m5_flg_colocaciones,  0) AS m5_flg_colocaciones,
        COALESCE(bp.m6_flg_colocaciones,  0) AS m6_flg_colocaciones,
        COALESCE(bp.m7_flg_colocaciones,  0) AS m7_flg_colocaciones,
        COALESCE(bp.m8_flg_colocaciones,  0) AS m8_flg_colocaciones,
        COALESCE(bp.m9_flg_colocaciones,  0) AS m9_flg_colocaciones,
        COALESCE(bp.m10_flg_colocaciones, 0) AS m10_flg_colocaciones,
        COALESCE(bp.m11_flg_colocaciones, 0) AS m11_flg_colocaciones,
        COALESCE(bp.m12_flg_colocaciones, 0) AS m12_flg_colocaciones,
        
        -- LAGS SALDO_PUNTA_PASIVO
        COALESCE(bp.m1_saldo_punta_pasivo,  0) AS m1_saldo_punta_pasivo,
        COALESCE(bp.m2_saldo_punta_pasivo,  0) AS m2_saldo_punta_pasivo,
        COALESCE(bp.m3_saldo_punta_pasivo,  0) AS m3_saldo_punta_pasivo,
        COALESCE(bp.m4_saldo_punta_pasivo,  0) AS m4_saldo_punta_pasivo,
        COALESCE(bp.m5_saldo_punta_pasivo,  0) AS m5_saldo_punta_pasivo,
        COALESCE(bp.m6_saldo_punta_pasivo,  0) AS m6_saldo_punta_pasivo,
        COALESCE(bp.m7_saldo_punta_pasivo,  0) AS m7_saldo_punta_pasivo,
        COALESCE(bp.m8_saldo_punta_pasivo,  0) AS m8_saldo_punta_pasivo,
        COALESCE(bp.m9_saldo_punta_pasivo,  0) AS m9_saldo_punta_pasivo,
        COALESCE(bp.m10_saldo_punta_pasivo, 0) AS m10_saldo_punta_pasivo,
        COALESCE(bp.m11_saldo_punta_pasivo, 0) AS m11_saldo_punta_pasivo,
        COALESCE(bp.m12_saldo_punta_pasivo, 0) AS m12_saldo_punta_pasivo,
        
        -- LAGS SALDO_PROMEDIO_PASIVO
        COALESCE(bp.m1_saldo_promedio_pasivo,  0) AS m1_saldo_promedio_pasivo,
        COALESCE(bp.m2_saldo_promedio_pasivo,  0) AS m2_saldo_promedio_pasivo,
        COALESCE(bp.m3_saldo_promedio_pasivo,  0) AS m3_saldo_promedio_pasivo,
        COALESCE(bp.m4_saldo_promedio_pasivo,  0) AS m4_saldo_promedio_pasivo,
        COALESCE(bp.m5_saldo_promedio_pasivo,  0) AS m5_saldo_promedio_pasivo,
        COALESCE(bp.m6_saldo_promedio_pasivo,  0) AS m6_saldo_promedio_pasivo,
        COALESCE(bp.m7_saldo_promedio_pasivo,  0) AS m7_saldo_promedio_pasivo,
        COALESCE(bp.m8_saldo_promedio_pasivo,  0) AS m8_saldo_promedio_pasivo,
        COALESCE(bp.m9_saldo_promedio_pasivo,  0) AS m9_saldo_promedio_pasivo,
        COALESCE(bp.m10_saldo_promedio_pasivo, 0) AS m10_saldo_promedio_pasivo,
        COALESCE(bp.m11_saldo_promedio_pasivo, 0) AS m11_saldo_promedio_pasivo,
        COALESCE(bp.m12_saldo_promedio_pasivo, 0) AS m12_saldo_promedio_pasivo,
        
        -- LAGS SALDO_PROMEDIO_COLOCACIONES
        COALESCE(bp.m1_saldo_promedio_colocaciones,  0) AS m1_saldo_promedio_colocaciones,
        COALESCE(bp.m2_saldo_promedio_colocaciones,  0) AS m2_saldo_promedio_colocaciones,
        COALESCE(bp.m3_saldo_promedio_colocaciones,  0) AS m3_saldo_promedio_colocaciones,
        COALESCE(bp.m4_saldo_promedio_colocaciones,  0) AS m4_saldo_promedio_colocaciones,
        COALESCE(bp.m5_saldo_promedio_colocaciones,  0) AS m5_saldo_promedio_colocaciones,
        COALESCE(bp.m6_saldo_promedio_colocaciones,  0) AS m6_saldo_promedio_colocaciones,
        COALESCE(bp.m7_saldo_promedio_colocaciones,  0) AS m7_saldo_promedio_colocaciones,
        COALESCE(bp.m8_saldo_promedio_colocaciones,  0) AS m8_saldo_promedio_colocaciones,
        COALESCE(bp.m9_saldo_promedio_colocaciones,  0) AS m9_saldo_promedio_colocaciones,
        COALESCE(bp.m10_saldo_promedio_colocaciones, 0) AS m10_saldo_promedio_colocaciones,
        COALESCE(bp.m11_saldo_promedio_colocaciones, 0) AS m11_saldo_promedio_colocaciones,
        COALESCE(bp.m12_saldo_promedio_colocaciones, 0) AS m12_saldo_promedio_colocaciones,
        
        -- LAGS SALDO_PUNTA_COLOCACIONES
        COALESCE(bp.m1_saldo_punta_colocaciones,  0) AS m1_saldo_punta_colocaciones,
        COALESCE(bp.m2_saldo_punta_colocaciones,  0) AS m2_saldo_punta_colocaciones,
        COALESCE(bp.m3_saldo_punta_colocaciones,  0) AS m3_saldo_punta_colocaciones,
        COALESCE(bp.m4_saldo_punta_colocaciones,  0) AS m4_saldo_punta_colocaciones,
        COALESCE(bp.m5_saldo_punta_colocaciones,  0) AS m5_saldo_punta_colocaciones,
        COALESCE(bp.m6_saldo_punta_colocaciones,  0) AS m6_saldo_punta_colocaciones,
        COALESCE(bp.m7_saldo_punta_colocaciones,  0) AS m7_saldo_punta_colocaciones,
        COALESCE(bp.m8_saldo_punta_colocaciones,  0) AS m8_saldo_punta_colocaciones,
        COALESCE(bp.m9_saldo_punta_colocaciones,  0) AS m9_saldo_punta_colocaciones,
        COALESCE(bp.m10_saldo_punta_colocaciones, 0) AS m10_saldo_punta_colocaciones,
        COALESCE(bp.m11_saldo_punta_colocaciones, 0) AS m11_saldo_punta_colocaciones,
        COALESCE(bp.m12_saldo_punta_colocaciones, 0) AS m12_saldo_punta_colocaciones,
        
        -- LAGS NRO_TIPO_PRODUCTO
        COALESCE(bp.m1_nro_tipo_producto,  0) AS m1_nro_tipo_producto,
        COALESCE(bp.m2_nro_tipo_producto,  0) AS m2_nro_tipo_producto,
        COALESCE(bp.m3_nro_tipo_producto,  0) AS m3_nro_tipo_producto,
        COALESCE(bp.m4_nro_tipo_producto,  0) AS m4_nro_tipo_producto,
        COALESCE(bp.m5_nro_tipo_producto,  0) AS m5_nro_tipo_producto,
        COALESCE(bp.m6_nro_tipo_producto,  0) AS m6_nro_tipo_producto,
        COALESCE(bp.m7_nro_tipo_producto,  0) AS m7_nro_tipo_producto,
        COALESCE(bp.m8_nro_tipo_producto,  0) AS m8_nro_tipo_producto,
        COALESCE(bp.m9_nro_tipo_producto,  0) AS m9_nro_tipo_producto,
        COALESCE(bp.m10_nro_tipo_producto, 0) AS m10_nro_tipo_producto,
        COALESCE(bp.m11_nro_tipo_producto, 0) AS m11_nro_tipo_producto,
        COALESCE(bp.m12_nro_tipo_producto, 0) AS m12_nro_tipo_producto,
        
        -- ==================== CALIDAD DE CARTERA ====================
        COALESCE(bp.flg_vigente,    0) AS flg_vigente,
        COALESCE(bp.flg_vencido,    0) AS flg_vencido,
        COALESCE(bp.flg_castigado,  0) AS flg_castigado,
        COALESCE(bp.saldo_deteriorado, 0) AS saldo_deteriorado,
        
        -- Ratio mora sobre colocaciones
        CASE WHEN COALESCE(bp.saldo_punta_colocaciones, 0) > 0
             THEN ROUND(COALESCE(bp.saldo_deteriorado, 0) / bp.saldo_punta_colocaciones, 4)
             ELSE 0 END AS ratio_mora,
        
        -- Historial mora: ¿tuvo vencido en últimos 6 meses?
        GREATEST(
            COALESCE(bp.m1_flg_vencido, 0), COALESCE(bp.m2_flg_vencido, 0),
            COALESCE(bp.m3_flg_vencido, 0), COALESCE(bp.m4_flg_vencido, 0),
            COALESCE(bp.m5_flg_vencido, 0), COALESCE(bp.m6_flg_vencido, 0)
        ) AS flg_mora_historica_6m,
        
        -- Historial castigo: ¿tuvo castigo en últimos 6 meses?
        GREATEST(
            COALESCE(bp.m1_flg_castigado, 0), COALESCE(bp.m2_flg_castigado, 0),
            COALESCE(bp.m3_flg_castigado, 0), COALESCE(bp.m4_flg_castigado, 0),
            COALESCE(bp.m5_flg_castigado, 0), COALESCE(bp.m6_flg_castigado, 0)
        ) AS flg_castigado_historico_6m,
        
        -- ==================== DOLARIZACIÓN ====================
        COALESCE(bp.saldo_pasivo_usd,       0) AS saldo_pasivo_usd,
        COALESCE(bp.saldo_colocaciones_usd, 0) AS saldo_colocaciones_usd,
        
        CASE WHEN COALESCE(bp.saldo_punta_pasivo, 0) > 0
             THEN ROUND(COALESCE(bp.saldo_pasivo_usd, 0) / bp.saldo_punta_pasivo, 4)
             ELSE 0 END AS ratio_dolarizacion_pasivo,
        
        CASE WHEN COALESCE(bp.saldo_punta_colocaciones, 0) > 0
             THEN ROUND(COALESCE(bp.saldo_colocaciones_usd, 0) / bp.saldo_punta_colocaciones, 4)
             ELSE 0 END AS ratio_dolarizacion_colocaciones,
        
        -- Flag bimonetario (cliente opera en PEN y USD)
        CASE WHEN COALESCE(bp.nro_monedas, 0) > 1 THEN 1 ELSE 0 END AS flg_bimonetario,
        
        -- ==================== RATIOS CRUZADOS ====================
        -- Cliente tiene ambos productos activos
        CASE WHEN COALESCE(bp.flg_pasivo, 0) = 1
              AND COALESCE(bp.flg_colocaciones, 0) = 1
             THEN 1 ELSE 0 END AS flg_vinculado,
        
        -- Ratio colocaciones sobre pasivo (apalancamiento relativo)
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > 0
             THEN ROUND(COALESCE(bp.saldo_promedio_colocaciones, 0) / bp.saldo_promedio_pasivo, 4)
             ELSE 0 END AS ratio_coloc_sobre_pasivo,
        
        -- ==================== VARIACIONES ABSOLUTAS ====================
        COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m1_saldo_promedio_pasivo,  0) AS var_abs_pasivo_1m,
        COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m3_saldo_promedio_pasivo,  0) AS var_abs_pasivo_3m,
        COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m6_saldo_promedio_pasivo,  0) AS var_abs_pasivo_6m,
        COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m12_saldo_promedio_pasivo, 0) AS var_abs_pasivo_12m,
        
        COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m1_saldo_promedio_colocaciones,  0) AS var_abs_colocaciones_1m,
        COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m3_saldo_promedio_colocaciones,  0) AS var_abs_colocaciones_3m,
        COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m6_saldo_promedio_colocaciones,  0) AS var_abs_colocaciones_6m,
        COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m12_saldo_promedio_colocaciones, 0) AS var_abs_colocaciones_12m,
        
        -- ==================== VARIACIONES PORCENTUALES ====================
        CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m1_saldo_promedio_pasivo, 0)) / bp.m1_saldo_promedio_pasivo) * 100, 2)
             ELSE 0 END AS var_pct_pasivo_1m,
        CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m3_saldo_promedio_pasivo, 0)) / bp.m3_saldo_promedio_pasivo) * 100, 2)
             ELSE 0 END AS var_pct_pasivo_3m,
        CASE WHEN COALESCE(bp.m6_saldo_promedio_pasivo, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m6_saldo_promedio_pasivo, 0)) / bp.m6_saldo_promedio_pasivo) * 100, 2)
             ELSE 0 END AS var_pct_pasivo_6m,
        CASE WHEN COALESCE(bp.m12_saldo_promedio_pasivo, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_pasivo, 0) - COALESCE(bp.m12_saldo_promedio_pasivo, 0)) / bp.m12_saldo_promedio_pasivo) * 100, 2)
             ELSE 0 END AS var_pct_pasivo_12m,
        
        CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m1_saldo_promedio_colocaciones, 0)) / bp.m1_saldo_promedio_colocaciones) * 100, 2)
             ELSE 0 END AS var_pct_colocaciones_1m,
        CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m3_saldo_promedio_colocaciones, 0)) / bp.m3_saldo_promedio_colocaciones) * 100, 2)
             ELSE 0 END AS var_pct_colocaciones_3m,
        CASE WHEN COALESCE(bp.m6_saldo_promedio_colocaciones, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m6_saldo_promedio_colocaciones, 0)) / bp.m6_saldo_promedio_colocaciones) * 100, 2)
             ELSE 0 END AS var_pct_colocaciones_6m,
        CASE WHEN COALESCE(bp.m12_saldo_promedio_colocaciones, 0) > 0
             THEN ROUND(((COALESCE(bp.saldo_promedio_colocaciones, 0) - COALESCE(bp.m12_saldo_promedio_colocaciones, 0)) / bp.m12_saldo_promedio_colocaciones) * 100, 2)
             ELSE 0 END AS var_pct_colocaciones_12m,
        
        -- ==================== PROMEDIOS MÓVILES ====================
        COALESCE(ROUND((COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.m1_saldo_promedio_pasivo, 0) + COALESCE(bp.m2_saldo_promedio_pasivo, 0)) /
            NULLIF((CASE WHEN bp.saldo_promedio_pasivo       IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m1_saldo_promedio_pasivo    IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m2_saldo_promedio_pasivo    IS NOT NULL THEN 1 ELSE 0 END), 0), 2), 0) AS promedio_movil_pasivo_3m,
        
        COALESCE(ROUND((COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.m1_saldo_promedio_pasivo, 0) + COALESCE(bp.m2_saldo_promedio_pasivo, 0) +
                        COALESCE(bp.m3_saldo_promedio_pasivo, 0) + COALESCE(bp.m4_saldo_promedio_pasivo, 0) + COALESCE(bp.m5_saldo_promedio_pasivo, 0)) /
            NULLIF((CASE WHEN bp.saldo_promedio_pasivo    IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m1_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m2_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m3_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m4_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m5_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END), 0), 2), 0) AS promedio_movil_pasivo_6m,
        
        COALESCE(ROUND((COALESCE(bp.saldo_promedio_pasivo, 0)    + COALESCE(bp.m1_saldo_promedio_pasivo, 0)  + COALESCE(bp.m2_saldo_promedio_pasivo, 0) +
                        COALESCE(bp.m3_saldo_promedio_pasivo, 0) + COALESCE(bp.m4_saldo_promedio_pasivo, 0)  + COALESCE(bp.m5_saldo_promedio_pasivo, 0) +
                        COALESCE(bp.m6_saldo_promedio_pasivo, 0) + COALESCE(bp.m7_saldo_promedio_pasivo, 0)  + COALESCE(bp.m8_saldo_promedio_pasivo, 0) +
                        COALESCE(bp.m9_saldo_promedio_pasivo, 0) + COALESCE(bp.m10_saldo_promedio_pasivo, 0) + COALESCE(bp.m11_saldo_promedio_pasivo, 0)) /
            NULLIF((CASE WHEN bp.saldo_promedio_pasivo    IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m1_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m2_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m3_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m4_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m5_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m6_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m7_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m8_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m9_saldo_promedio_pasivo  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m10_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m11_saldo_promedio_pasivo IS NOT NULL THEN 1 ELSE 0 END), 0), 2), 0) AS promedio_movil_pasivo_12m,
        
        COALESCE(ROUND((COALESCE(bp.saldo_promedio_colocaciones, 0) + COALESCE(bp.m1_saldo_promedio_colocaciones, 0) + COALESCE(bp.m2_saldo_promedio_colocaciones, 0)) /
            NULLIF((CASE WHEN bp.saldo_promedio_colocaciones    IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m1_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m2_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END), 0), 2), 0) AS promedio_movil_colocaciones_3m,
        
        COALESCE(ROUND((COALESCE(bp.saldo_promedio_colocaciones, 0) + COALESCE(bp.m1_saldo_promedio_colocaciones, 0) + COALESCE(bp.m2_saldo_promedio_colocaciones, 0) +
                        COALESCE(bp.m3_saldo_promedio_colocaciones, 0) + COALESCE(bp.m4_saldo_promedio_colocaciones, 0) + COALESCE(bp.m5_saldo_promedio_colocaciones, 0)) /
            NULLIF((CASE WHEN bp.saldo_promedio_colocaciones    IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m1_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m2_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m3_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m4_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m5_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END), 0), 2), 0) AS promedio_movil_colocaciones_6m,
        
        COALESCE(ROUND((COALESCE(bp.saldo_promedio_colocaciones, 0)    + COALESCE(bp.m1_saldo_promedio_colocaciones, 0)  + COALESCE(bp.m2_saldo_promedio_colocaciones, 0) +
                        COALESCE(bp.m3_saldo_promedio_colocaciones, 0) + COALESCE(bp.m4_saldo_promedio_colocaciones, 0)  + COALESCE(bp.m5_saldo_promedio_colocaciones, 0) +
                        COALESCE(bp.m6_saldo_promedio_colocaciones, 0) + COALESCE(bp.m7_saldo_promedio_colocaciones, 0)  + COALESCE(bp.m8_saldo_promedio_colocaciones, 0) +
                        COALESCE(bp.m9_saldo_promedio_colocaciones, 0) + COALESCE(bp.m10_saldo_promedio_colocaciones, 0) + COALESCE(bp.m11_saldo_promedio_colocaciones, 0)) /
            NULLIF((CASE WHEN bp.saldo_promedio_colocaciones    IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m1_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m2_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m3_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m4_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m5_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m6_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m7_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m8_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m9_saldo_promedio_colocaciones  IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m10_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN bp.m11_saldo_promedio_colocaciones IS NOT NULL THEN 1 ELSE 0 END), 0), 2), 0) AS promedio_movil_colocaciones_12m,
        
        -- ==================== RECENCIA ====================
        CASE
            WHEN COALESCE(bp.saldo_promedio_pasivo,    0) > 0 THEN 0
            WHEN COALESCE(bp.m1_saldo_promedio_pasivo, 0) > 0 THEN 1
            WHEN COALESCE(bp.m2_saldo_promedio_pasivo, 0) > 0 THEN 2
            WHEN COALESCE(bp.m3_saldo_promedio_pasivo, 0) > 0 THEN 3
            WHEN COALESCE(bp.m4_saldo_promedio_pasivo, 0) > 0 THEN 4
            WHEN COALESCE(bp.m5_saldo_promedio_pasivo, 0) > 0 THEN 5
            WHEN COALESCE(bp.m6_saldo_promedio_pasivo, 0) > 0 THEN 6
            WHEN COALESCE(bp.m7_saldo_promedio_pasivo, 0) > 0 THEN 7
            WHEN COALESCE(bp.m8_saldo_promedio_pasivo, 0) > 0 THEN 8
            WHEN COALESCE(bp.m9_saldo_promedio_pasivo, 0) > 0 THEN 9
            WHEN COALESCE(bp.m10_saldo_promedio_pasivo, 0) > 0 THEN 10
            WHEN COALESCE(bp.m11_saldo_promedio_pasivo, 0) > 0 THEN 11
            WHEN COALESCE(bp.m12_saldo_promedio_pasivo, 0) > 0 THEN 12
            ELSE 13
        END AS recencia_pasivo_meses,
        
        CASE
            WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) > 0 THEN 0
            WHEN COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > 0 THEN 1
            WHEN COALESCE(bp.m2_saldo_promedio_colocaciones, 0) > 0 THEN 2
            WHEN COALESCE(bp.m3_saldo_promedio_colocaciones, 0) > 0 THEN 3
            WHEN COALESCE(bp.m4_saldo_promedio_colocaciones, 0) > 0 THEN 4
            WHEN COALESCE(bp.m5_saldo_promedio_colocaciones, 0) > 0 THEN 5
            WHEN COALESCE(bp.m6_saldo_promedio_colocaciones, 0) > 0 THEN 6
            WHEN COALESCE(bp.m7_saldo_promedio_colocaciones, 0) > 0 THEN 7
            WHEN COALESCE(bp.m8_saldo_promedio_colocaciones, 0) > 0 THEN 8
            WHEN COALESCE(bp.m9_saldo_promedio_colocaciones, 0) > 0 THEN 9
            WHEN COALESCE(bp.m10_saldo_promedio_colocaciones, 0) > 0 THEN 10
            WHEN COALESCE(bp.m11_saldo_promedio_colocaciones, 0) > 0 THEN 11
            WHEN COALESCE(bp.m12_saldo_promedio_colocaciones, 0) > 0 THEN 12
            ELSE 13
        END AS recencia_colocaciones_meses,
        
        CASE
            WHEN COALESCE(bp.saldo_promedio_pasivo,     0) > 10000 THEN 0
            WHEN COALESCE(bp.m1_saldo_promedio_pasivo,  0) > 10000 THEN 1
            WHEN COALESCE(bp.m2_saldo_promedio_pasivo,  0) > 10000 THEN 2
            WHEN COALESCE(bp.m3_saldo_promedio_pasivo,  0) > 10000 THEN 3
            WHEN COALESCE(bp.m4_saldo_promedio_pasivo,  0) > 10000 THEN 4
            WHEN COALESCE(bp.m5_saldo_promedio_pasivo,  0) > 10000 THEN 5
            WHEN COALESCE(bp.m6_saldo_promedio_pasivo,  0) > 10000 THEN 6
            WHEN COALESCE(bp.m7_saldo_promedio_pasivo,  0) > 10000 THEN 7
            WHEN COALESCE(bp.m8_saldo_promedio_pasivo,  0) > 10000 THEN 8
            WHEN COALESCE(bp.m9_saldo_promedio_pasivo,  0) > 10000 THEN 9
            WHEN COALESCE(bp.m10_saldo_promedio_pasivo, 0) > 10000 THEN 10
            WHEN COALESCE(bp.m11_saldo_promedio_pasivo, 0) > 10000 THEN 11
            WHEN COALESCE(bp.m12_saldo_promedio_pasivo, 0) > 10000 THEN 12
            ELSE 13
        END AS recencia_pasivo_mayor_10k_meses,
        
        CASE
            WHEN COALESCE(bp.saldo_promedio_colocaciones,     0) > 10000 THEN 0
            WHEN COALESCE(bp.m1_saldo_promedio_colocaciones,  0) > 10000 THEN 1
            WHEN COALESCE(bp.m2_saldo_promedio_colocaciones,  0) > 10000 THEN 2
            WHEN COALESCE(bp.m3_saldo_promedio_colocaciones,  0) > 10000 THEN 3
            WHEN COALESCE(bp.m4_saldo_promedio_colocaciones,  0) > 10000 THEN 4
            WHEN COALESCE(bp.m5_saldo_promedio_colocaciones,  0) > 10000 THEN 5
            WHEN COALESCE(bp.m6_saldo_promedio_colocaciones,  0) > 10000 THEN 6
            WHEN COALESCE(bp.m7_saldo_promedio_colocaciones,  0) > 10000 THEN 7
            WHEN COALESCE(bp.m8_saldo_promedio_colocaciones,  0) > 10000 THEN 8
            WHEN COALESCE(bp.m9_saldo_promedio_colocaciones,  0) > 10000 THEN 9
            WHEN COALESCE(bp.m10_saldo_promedio_colocaciones, 0) > 10000 THEN 10
            WHEN COALESCE(bp.m11_saldo_promedio_colocaciones, 0) > 10000 THEN 11
            WHEN COALESCE(bp.m12_saldo_promedio_colocaciones, 0) > 10000 THEN 12
            ELSE 13
        END AS recencia_colocaciones_mayor_10k_meses,
        
        -- ==================== FRECUENCIA ====================
        COALESCE(ROUND((CASE WHEN COALESCE(bp.saldo_promedio_pasivo,    0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m2_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m4_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m5_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END) / 6.0 * 100, 2), 0) AS frecuencia_pasivo_pct_6m,
        
        COALESCE(ROUND((CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m2_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m4_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m5_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END) / 6.0 * 100, 2), 0) AS frecuencia_colocaciones_pct_6m,
        
        COALESCE(ROUND((CASE WHEN COALESCE(bp.saldo_promedio_pasivo,     0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m2_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m4_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m5_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m6_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m7_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m8_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m9_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m10_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m11_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END) / 12.0 * 100, 2), 0) AS frecuencia_pasivo_pct_12m,
        
        COALESCE(ROUND((CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,     0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m2_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m4_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m5_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m6_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m7_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m8_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m9_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m10_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m11_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END) / 12.0 * 100, 2), 0) AS frecuencia_colocaciones_pct_12m,
        
        COALESCE(ROUND((CASE WHEN COALESCE(bp.saldo_promedio_pasivo,    0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m2_saldo_promedio_pasivo, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m4_saldo_promedio_pasivo, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m5_saldo_promedio_pasivo, 0) > 10000 THEN 1 ELSE 0 END) / 6.0 * 100, 2), 0) AS frecuencia_pasivo_mayor_10k_pct_6m,
        
        COALESCE(ROUND((CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m2_saldo_promedio_colocaciones, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m4_saldo_promedio_colocaciones, 0) > 10000 THEN 1 ELSE 0 END +
                         CASE WHEN COALESCE(bp.m5_saldo_promedio_colocaciones, 0) > 10000 THEN 1 ELSE 0 END) / 6.0 * 100, 2), 0) AS frecuencia_colocaciones_mayor_10k_pct_6m,
        
        -- Meses consecutivos de crecimiento en pasivo (últimos 6m)
        (CASE WHEN COALESCE(bp.saldo_promedio_pasivo,    0) > COALESCE(bp.m1_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo, 0) > COALESCE(bp.m2_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m2_saldo_promedio_pasivo, 0) > COALESCE(bp.m3_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo, 0) > COALESCE(bp.m4_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m4_saldo_promedio_pasivo, 0) > COALESCE(bp.m5_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m5_saldo_promedio_pasivo, 0) > COALESCE(bp.m6_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END) AS frecuencia_crecimiento_pasivo_6m,
        
        -- Meses consecutivos de crecimiento en colocaciones (últimos 6m)
        (CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) > COALESCE(bp.m1_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > COALESCE(bp.m2_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m2_saldo_promedio_colocaciones, 0) > COALESCE(bp.m3_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones, 0) > COALESCE(bp.m4_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m4_saldo_promedio_colocaciones, 0) > COALESCE(bp.m5_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m5_saldo_promedio_colocaciones, 0) > COALESCE(bp.m6_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END) AS frecuencia_crecimiento_colocaciones_6m,
        
        -- ==================== MESES CON SALDO ====================
        (CASE WHEN COALESCE(bp.saldo_promedio_pasivo,    0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m2_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m4_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m5_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END) AS meses_con_saldo_pasivo_6m,
        
        (CASE WHEN COALESCE(bp.saldo_promedio_pasivo,     0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m1_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m2_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m3_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m4_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m5_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m6_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m7_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m8_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m9_saldo_promedio_pasivo,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m10_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m11_saldo_promedio_pasivo, 0) > 0 THEN 1 ELSE 0 END) AS meses_con_saldo_pasivo_12m,
        
        (CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m2_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m4_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m5_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END) AS meses_con_saldo_colocaciones_6m,
        
        (CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,     0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m1_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m2_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m3_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m4_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m5_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m6_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m7_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m8_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m9_saldo_promedio_colocaciones,  0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m10_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END +
         CASE WHEN COALESCE(bp.m11_saldo_promedio_colocaciones, 0) > 0 THEN 1 ELSE 0 END) AS meses_con_saldo_colocaciones_12m,
        
        -- ==================== SALDOS TOTALES ====================
        COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.saldo_promedio_colocaciones, 0) AS saldo_total,
        COALESCE(bp.saldo_punta_pasivo, 0)    + COALESCE(bp.saldo_punta_colocaciones, 0)    AS saldo_punta_total,
        
        -- Headroom: gap entre máximo histórico 12m y saldo actual
        GREATEST(
            COALESCE(bp.saldo_promedio_colocaciones, 0), COALESCE(bp.m1_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m2_saldo_promedio_colocaciones, 0), COALESCE(bp.m3_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m4_saldo_promedio_colocaciones, 0), COALESCE(bp.m5_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m6_saldo_promedio_colocaciones, 0), COALESCE(bp.m7_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m8_saldo_promedio_colocaciones, 0), COALESCE(bp.m9_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m10_saldo_promedio_colocaciones, 0), COALESCE(bp.m11_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m12_saldo_promedio_colocaciones, 0)
        ) - COALESCE(bp.saldo_promedio_colocaciones, 0)                                      AS headroom_colocaciones_12m,
        
        -- ==================== FLAGS DE TENDENCIA ====================
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo,    0) > COALESCE(bp.m1_saldo_promedio_pasivo, 0)
              AND COALESCE(bp.m1_saldo_promedio_pasivo, 0) > COALESCE(bp.m2_saldo_promedio_pasivo, 0)
              AND COALESCE(bp.m2_saldo_promedio_pasivo, 0) > COALESCE(bp.m3_saldo_promedio_pasivo, 0)
             THEN 1 ELSE 0 END AS flag_crecimiento_pasivo_sostenido_3m,
        
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) > COALESCE(bp.m1_saldo_promedio_colocaciones, 0)
              AND COALESCE(bp.m1_saldo_promedio_colocaciones, 0) > COALESCE(bp.m2_saldo_promedio_colocaciones, 0)
              AND COALESCE(bp.m2_saldo_promedio_colocaciones, 0) > COALESCE(bp.m3_saldo_promedio_colocaciones, 0)
             THEN 1 ELSE 0 END AS flag_crecimiento_colocaciones_sostenido_3m,
        
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo,    0) < COALESCE(bp.m1_saldo_promedio_pasivo, 0)
              AND COALESCE(bp.m1_saldo_promedio_pasivo, 0) < COALESCE(bp.m2_saldo_promedio_pasivo, 0)
              AND COALESCE(bp.m2_saldo_promedio_pasivo, 0) < COALESCE(bp.m3_saldo_promedio_pasivo, 0)
             THEN 1 ELSE 0 END AS flag_decrecimiento_pasivo_sostenido_3m,
        
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones,    0) < COALESCE(bp.m1_saldo_promedio_colocaciones, 0)
              AND COALESCE(bp.m1_saldo_promedio_colocaciones, 0) < COALESCE(bp.m2_saldo_promedio_colocaciones, 0)
              AND COALESCE(bp.m2_saldo_promedio_colocaciones, 0) < COALESCE(bp.m3_saldo_promedio_colocaciones, 0)
             THEN 1 ELSE 0 END AS flag_decrecimiento_colocaciones_sostenido_3m,
        
        -- ==================== FLAGS CRECIMIENTO / DECRECIMIENTO ====================
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > COALESCE(bp.m1_saldo_promedio_pasivo,  0) THEN 1 ELSE 0 END AS flg_pasivo_crece_1m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) < COALESCE(bp.m1_saldo_promedio_pasivo,  0) THEN 1 ELSE 0 END AS flg_pasivo_decrece_1m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > COALESCE(bp.m3_saldo_promedio_pasivo,  0) THEN 1 ELSE 0 END AS flg_pasivo_crece_3m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) < COALESCE(bp.m3_saldo_promedio_pasivo,  0) THEN 1 ELSE 0 END AS flg_pasivo_decrece_3m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > COALESCE(bp.m6_saldo_promedio_pasivo,  0) THEN 1 ELSE 0 END AS flg_pasivo_crece_6m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) < COALESCE(bp.m6_saldo_promedio_pasivo,  0) THEN 1 ELSE 0 END AS flg_pasivo_decrece_6m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > COALESCE(bp.m12_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END AS flg_pasivo_crece_12m,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) < COALESCE(bp.m12_saldo_promedio_pasivo, 0) THEN 1 ELSE 0 END AS flg_pasivo_decrece_12m,
        
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > COALESCE(bp.m1_saldo_promedio_colocaciones,  0) THEN 1 ELSE 0 END AS flg_colocaciones_crece_1m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) < COALESCE(bp.m1_saldo_promedio_colocaciones,  0) THEN 1 ELSE 0 END AS flg_colocaciones_decrece_1m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > COALESCE(bp.m3_saldo_promedio_colocaciones,  0) THEN 1 ELSE 0 END AS flg_colocaciones_crece_3m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) < COALESCE(bp.m3_saldo_promedio_colocaciones,  0) THEN 1 ELSE 0 END AS flg_colocaciones_decrece_3m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > COALESCE(bp.m6_saldo_promedio_colocaciones,  0) THEN 1 ELSE 0 END AS flg_colocaciones_crece_6m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) < COALESCE(bp.m6_saldo_promedio_colocaciones,  0) THEN 1 ELSE 0 END AS flg_colocaciones_decrece_6m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > COALESCE(bp.m12_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END AS flg_colocaciones_crece_12m,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) < COALESCE(bp.m12_saldo_promedio_colocaciones, 0) THEN 1 ELSE 0 END AS flg_colocaciones_decrece_12m,
        
        -- ==================== FLAGS DE UMBRALES ====================
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > 1000   THEN 1 ELSE 0 END AS flg_pasivo_mayor_1k,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > 10000  THEN 1 ELSE 0 END AS flg_pasivo_mayor_10k,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > 50000  THEN 1 ELSE 0 END AS flg_pasivo_mayor_50k,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > 100000 THEN 1 ELSE 0 END AS flg_pasivo_mayor_100k,
        CASE WHEN COALESCE(bp.saldo_promedio_pasivo, 0) > 500000 THEN 1 ELSE 0 END AS flg_pasivo_mayor_500k,
        
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > 1000   THEN 1 ELSE 0 END AS flg_colocaciones_mayor_1k,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > 10000  THEN 1 ELSE 0 END AS flg_colocaciones_mayor_10k,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > 50000  THEN 1 ELSE 0 END AS flg_colocaciones_mayor_50k,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > 100000 THEN 1 ELSE 0 END AS flg_colocaciones_mayor_100k,
        CASE WHEN COALESCE(bp.saldo_promedio_colocaciones, 0) > 500000 THEN 1 ELSE 0 END AS flg_colocaciones_mayor_500k,
        
        CASE WHEN (COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.saldo_promedio_colocaciones, 0)) > 1000   THEN 1 ELSE 0 END AS flg_saldo_total_mayor_1k,
        CASE WHEN (COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.saldo_promedio_colocaciones, 0)) > 10000  THEN 1 ELSE 0 END AS flg_saldo_total_mayor_10k,
        CASE WHEN (COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.saldo_promedio_colocaciones, 0)) > 50000  THEN 1 ELSE 0 END AS flg_saldo_total_mayor_50k,
        CASE WHEN (COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.saldo_promedio_colocaciones, 0)) > 100000 THEN 1 ELSE 0 END AS flg_saldo_total_mayor_100k,
        CASE WHEN (COALESCE(bp.saldo_promedio_pasivo, 0) + COALESCE(bp.saldo_promedio_colocaciones, 0)) > 500000 THEN 1 ELSE 0 END AS flg_saldo_total_mayor_500k,
        
        -- ==================== MÁXIMOS Y MÍNIMOS ====================
        GREATEST(
            COALESCE(bp.saldo_promedio_pasivo,     0), COALESCE(bp.m1_saldo_promedio_pasivo,  0),
            COALESCE(bp.m2_saldo_promedio_pasivo,  0), COALESCE(bp.m3_saldo_promedio_pasivo,  0),
            COALESCE(bp.m4_saldo_promedio_pasivo,  0), COALESCE(bp.m5_saldo_promedio_pasivo,  0),
            COALESCE(bp.m6_saldo_promedio_pasivo,  0), COALESCE(bp.m7_saldo_promedio_pasivo,  0),
            COALESCE(bp.m8_saldo_promedio_pasivo,  0), COALESCE(bp.m9_saldo_promedio_pasivo,  0),
            COALESCE(bp.m10_saldo_promedio_pasivo, 0), COALESCE(bp.m11_saldo_promedio_pasivo, 0),
            COALESCE(bp.m12_saldo_promedio_pasivo, 0)
        ) AS max_saldo_pasivo_12m,
        
        GREATEST(
            COALESCE(bp.saldo_promedio_colocaciones,     0), COALESCE(bp.m1_saldo_promedio_colocaciones,  0),
            COALESCE(bp.m2_saldo_promedio_colocaciones,  0), COALESCE(bp.m3_saldo_promedio_colocaciones,  0),
            COALESCE(bp.m4_saldo_promedio_colocaciones,  0), COALESCE(bp.m5_saldo_promedio_colocaciones,  0),
            COALESCE(bp.m6_saldo_promedio_colocaciones,  0), COALESCE(bp.m7_saldo_promedio_colocaciones,  0),
            COALESCE(bp.m8_saldo_promedio_colocaciones,  0), COALESCE(bp.m9_saldo_promedio_colocaciones,  0),
            COALESCE(bp.m10_saldo_promedio_colocaciones, 0), COALESCE(bp.m11_saldo_promedio_colocaciones, 0),
            COALESCE(bp.m12_saldo_promedio_colocaciones, 0)
        ) AS max_saldo_colocaciones_12m,
        
        COALESCE(LEAST(
            NULLIF(COALESCE(bp.saldo_promedio_pasivo,     0), 0), NULLIF(COALESCE(bp.m1_saldo_promedio_pasivo,  0), 0),
            NULLIF(COALESCE(bp.m2_saldo_promedio_pasivo,  0), 0), NULLIF(COALESCE(bp.m3_saldo_promedio_pasivo,  0), 0),
            NULLIF(COALESCE(bp.m4_saldo_promedio_pasivo,  0), 0), NULLIF(COALESCE(bp.m5_saldo_promedio_pasivo,  0), 0),
            NULLIF(COALESCE(bp.m6_saldo_promedio_pasivo,  0), 0), NULLIF(COALESCE(bp.m7_saldo_promedio_pasivo,  0), 0),
            NULLIF(COALESCE(bp.m8_saldo_promedio_pasivo,  0), 0), NULLIF(COALESCE(bp.m9_saldo_promedio_pasivo,  0), 0),
            NULLIF(COALESCE(bp.m10_saldo_promedio_pasivo, 0), 0), NULLIF(COALESCE(bp.m11_saldo_promedio_pasivo, 0), 0),
            NULLIF(COALESCE(bp.m12_saldo_promedio_pasivo, 0), 0)
        ), 0) AS min_saldo_pasivo_12m,
        
        COALESCE(LEAST(
            NULLIF(COALESCE(bp.saldo_promedio_colocaciones,     0), 0), NULLIF(COALESCE(bp.m1_saldo_promedio_colocaciones,  0), 0),
            NULLIF(COALESCE(bp.m2_saldo_promedio_colocaciones,  0), 0), NULLIF(COALESCE(bp.m3_saldo_promedio_colocaciones,  0), 0),
            NULLIF(COALESCE(bp.m4_saldo_promedio_colocaciones,  0), 0), NULLIF(COALESCE(bp.m5_saldo_promedio_colocaciones,  0), 0),
            NULLIF(COALESCE(bp.m6_saldo_promedio_colocaciones,  0), 0), NULLIF(COALESCE(bp.m7_saldo_promedio_colocaciones,  0), 0),
            NULLIF(COALESCE(bp.m8_saldo_promedio_colocaciones,  0), 0), NULLIF(COALESCE(bp.m9_saldo_promedio_colocaciones,  0), 0),
            NULLIF(COALESCE(bp.m10_saldo_promedio_colocaciones, 0), 0), NULLIF(COALESCE(bp.m11_saldo_promedio_colocaciones, 0), 0),
            NULLIF(COALESCE(bp.m12_saldo_promedio_colocaciones, 0), 0)
        ), 0) AS min_saldo_colocaciones_12m,
        
        bp.PERIODO_2
        
    FROM base_pivot bp
)
SELECT *
FROM features_adicionales
)"""


# ------------------------------ pasos ---------------------------------------
def build_universo(uni_inicio: int, uni_fin: int):
    print("[1] Universo maestro (" + str(uni_inicio) + "-" + str(uni_fin) + ")")
    drop_table("HM_UNIVERSO_MAESTRO_SALDOS")
    run_ddl(SQL_UNIVERSO.format(uni_inicio=uni_inicio, uni_fin=uni_fin), "universo")

def build_mes(periodo: int):
    inicio = add_months(periodo, -LAG_WINDOW)
    print("[mes] " + str(periodo) + " (ventana lags desde " + str(inicio) + ")")
    drop_table("HM_SALDO_VPC_IBK_PIVOT")
    run_ddl(SQL_PIVOT.format(periodo_objetivo=periodo, periodo_inicio=inicio), "pivot " + str(periodo))
    drop_table("HM_SALDO_VPC_IBK_FEATURES_" + str(periodo))
    run_ddl(SQL_FEATURES.format(periodo_objetivo=periodo), "features " + str(periodo))

def build_total(periodos):
    print("[3] Union total (" + str(len(periodos)) + " meses)")
    drop_table("HM_SALDO_VPC_IBK_TOTAL")
    unions = "\nUNION ALL\n".join(
        "SELECT * FROM " + DATABASE + ".HM_SALDO_VPC_IBK_FEATURES_" + str(p) for p in periodos
    )
    sql = ("CREATE TABLE " + DATABASE + ".HM_SALDO_VPC_IBK_TOTAL\n"
           "WITH (format='Parquet', parquet_compression='SNAPPY') AS (\n"
           + unions + "\n)")
    run_ddl(sql, "total")


def main():
    periodos = month_range(PERIODO_DESDE, PERIODO_HASTA)
    uni_inicio = add_months(PERIODO_DESDE, -LAG_WINDOW)
    print("Generando " + str(len(periodos)) + " meses: " + str(periodos[0]) + ".." + str(periodos[-1]))
    t0 = time.time()
    build_universo(uni_inicio, PERIODO_HASTA)
    for p in periodos:
        build_mes(p)
    build_total(periodos)
    print("Listo en " + str(round((time.time()-t0)/60, 1)) + " min. Tabla: " + DATABASE + ".HM_SALDO_VPC_IBK_TOTAL")


if __name__ == "__main__":
    main()
