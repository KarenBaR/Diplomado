-- ============================================================================
-- MODELO DE FUGA (compra de deuda) - NBC Empresas
-- TABLA DE ENTRENAMIENTO: features de nivel + features de TENDENCIA + target
-- ----------------------------------------------------------------------------
-- Grano        : RUC (numeroruc) x periodo mensual  (panel)
-- PO           : RUC con saldo vigente agregado > 50K en el corte t
-- Target       : y = 1 si lo compro otro banco en (t, t+h]   (h=3)
-- 2024 -> solo alimenta features (lags). Filas etiquetadas: 202502..202511.
-- Todas las features de tendencia son BACKWARD-LOOKING (sin leakage).
-- ============================================================================

-- CREATE TABLE disc_comercial.hm_train_fuga_bn WITH (format='PARQUET') AS

WITH
-- 0) Credito de mayor saldo (atributos "del principal") ---------------------
principal AS (
    SELECT periodo, numeroruc, producto_real AS producto_principal,
           CAST(tea_aprobada AS double) AS tea_principal
    FROM (
        SELECT periodo, numeroruc, producto_real, tea_aprobada,
               ROW_NUMBER() OVER (PARTITION BY periodo, numeroruc
                                  ORDER BY CAST(saldo_pri AS double) DESC) AS rn
        FROM disc_comercial.kbr_po_fuga_bn_hash
        WHERE estado_solcre='VIGENTE' AND CAST(saldo_pri AS double) > 0
    ) WHERE rn = 1
),

-- 1) Snapshot mensual COLAPSADO a nivel RUC (toda la historia) ---------------
cliente_mes AS (
    SELECT
        b.periodo, b.numeroruc, MAX(b.cod_unico) AS cod_unico,
        CAST(substr(b.periodo,1,4) AS integer)*12 + CAST(substr(b.periodo,5,2) AS integer) AS periodo_idx,
        -- exposicion
        SUM(CAST(b.saldo_pri AS double))                      AS saldo_total,
        COUNT(DISTINCT b.cod_credito)                         AS n_creditos,
        SUM(CAST(b.monto_desembolsado AS double))             AS monto_desem_total,
        SUM(CAST(b.saldo_pri AS double)) / NULLIF(SUM(CAST(b.monto_desembolsado AS double)),0) AS pct_saldo_remanente,
        -- tasa
        MAX(CAST(b.tea_aprobada AS double))                   AS tea_max,
        MIN(CAST(b.tea_aprobada AS double))                   AS tea_min,
        SUM(CAST(b.saldo_pri AS double)*CAST(b.tea_aprobada AS double)) / NULLIF(SUM(CAST(b.saldo_pri AS double)),0) AS tea_pond,
        -- antiguedad (meses desde desembolso)
        (CAST(substr(b.periodo,1,4) AS integer)*12 + CAST(substr(b.periodo,5,2) AS integer))
          - MAX(CASE WHEN substr(b.fecha_desembolsado,1,4)>'1900'
                     THEN CAST(substr(b.fecha_desembolsado,1,4) AS integer)*12 + CAST(substr(b.fecha_desembolsado,6,2) AS integer) END) AS meses_desde_desem_min,
        (CAST(substr(b.periodo,1,4) AS integer)*12 + CAST(substr(b.periodo,5,2) AS integer))
          - MIN(CASE WHEN substr(b.fecha_desembolsado,1,4)>'1900'
                     THEN CAST(substr(b.fecha_desembolsado,1,4) AS integer)*12 + CAST(substr(b.fecha_desembolsado,6,2) AS integer) END) AS meses_desde_desem_max,
        -- cuotas
        SUM(CAST(b.nro_cuotas AS integer))                    AS cuotas_total,
        SUM(CAST(b.nro_cuotas_pendientes AS integer))         AS cuotas_pend_total,
        1.0 - SUM(CAST(b.nro_cuotas_pendientes AS double)) / NULLIF(SUM(CAST(b.nro_cuotas AS double)),0) AS pct_avance_cuotas,
        SUM(CAST(b.cuota AS double))                          AS cuota_total,
        -- riesgo
        MAX(CAST(b.atraso_maxpag AS integer))                 AS atraso_max,
        MIN(CAST(b.puntaje_scoring AS double))                AS scoring_min,
        MAX(CAST(b.dias_venc AS integer))                     AS dias_venc_max,
        -- mix de producto (6 flags)
        MAX(CASE WHEN b.producto_real='CAPITAL DE TRABAJO' THEN 1 ELSE 0 END) AS flg_cap_trabajo,
        MAX(CASE WHEN b.producto_real='COMPRA DE DEUDA'    THEN 1 ELSE 0 END) AS flg_compra_deuda,
        MAX(CASE WHEN b.producto_real='ACTIVO FIJO'        THEN 1 ELSE 0 END) AS flg_activo_fijo,
        MAX(CASE WHEN b.producto_real='LINEA REVOLVENTE'   THEN 1 ELSE 0 END) AS flg_linea_revolv,
        MAX(CASE WHEN b.producto_real='ESTACIONAL'         THEN 1 ELSE 0 END) AS flg_estacional,
        MAX(CASE WHEN b.producto_real='LM ESTACIONAL'      THEN 1 ELSE 0 END) AS flg_lm_estacional
    FROM disc_comercial.kbr_po_fuga_bn_hash b
    WHERE b.estado_solcre='VIGENTE' AND CAST(b.saldo_pri AS double) > 0
    GROUP BY b.periodo, b.numeroruc
),

-- 2) Lags mes a mes (continuidad validada con el indice de periodo) ----------
lags AS (
    SELECT cm.*,
        periodo_idx - LAG(periodo_idx,1) OVER w  AS gap_1,
        periodo_idx - LAG(periodo_idx,3) OVER w  AS gap_3,
        periodo_idx - LAG(periodo_idx,6) OVER w  AS gap_6,
        LAG(saldo_total,1)       OVER w AS saldo_l1,
        LAG(saldo_total,3)       OVER w AS saldo_l3,
        LAG(saldo_total,6)       OVER w AS saldo_l6,
        LAG(tea_pond,3)          OVER w AS tea_pond_l3,
        LAG(n_creditos,3)        OVER w AS ncred_l3,
        LAG(cuotas_pend_total,1) OVER w AS cuotas_pend_l1,
        ROW_NUMBER()             OVER w AS meses_en_panel
    FROM cliente_mes cm
    WINDOW w AS (PARTITION BY numeroruc ORDER BY periodo_idx)
),

-- 3) Deltas por fila (solo validos si el lag esta a la distancia correcta) ---
deltas AS (
    SELECT l.*,
        CASE WHEN gap_3=3 THEN (saldo_total - saldo_l3)/NULLIF(saldo_l3,0) END AS pct_d_saldo_3m,
        CASE WHEN gap_6=6 THEN (saldo_total - saldo_l6)/NULLIF(saldo_l6,0) END AS pct_d_saldo_6m,
        CASE WHEN gap_3=3 THEN tea_pond - tea_pond_l3 END                      AS d_tea_pond_3m,
        CASE WHEN gap_3=3 THEN n_creditos - ncred_l3 END                       AS d_n_creditos_3m,
        -- prepago del mes: caida de saldo muy por encima de una cuota de capital
        CASE WHEN gap_1=1 THEN saldo_l1 - saldo_total END                      AS drop_1m,
        CASE WHEN gap_1=1 THEN saldo_l1 / NULLIF(cuotas_pend_l1,0) END         AS drop_esperado_1m
    FROM lags l
),
deltas2 AS (
    SELECT d.*,
        CASE WHEN drop_1m > 1.8*drop_esperado_1m THEN 1 ELSE 0 END AS prepago_mes,
        GREATEST(COALESCE(drop_1m,0) - COALESCE(drop_esperado_1m,0), 0) AS exceso_prepago_1m
    FROM deltas d
),

-- 4) Acumulados moviles (prepagos en ventana de 6 meses, max caida en 3m) ----
panel AS (
    SELECT d2.*,
        SUM(prepago_mes)       OVER w6 AS n_prepagos_6m,
        SUM(exceso_prepago_1m) OVER w6 AS monto_prepago_6m,
        MAX(CASE WHEN gap_1=1 THEN (saldo_l1 - saldo_total)/NULLIF(saldo_l1,0) END) OVER w3 AS max_caida_rel_3m
    FROM deltas2 d2
    WINDOW
        w6 AS (PARTITION BY numeroruc ORDER BY periodo_idx ROWS BETWEEN 5 PRECEDING AND CURRENT ROW),
        w3 AS (PARTITION BY numeroruc ORDER BY periodo_idx ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
),

-- 5) PO (>50K) + atributos del principal ------------------------------------
po AS (
    SELECT p.*, pr.producto_principal, pr.tea_principal
    FROM panel p
    LEFT JOIN principal pr ON p.periodo=pr.periodo AND p.numeroruc=pr.numeroruc
    WHERE p.saldo_total > 50000                                              -- <-- PARAM umbral_po
),

-- 6) Fuga por cliente (cruce por cod_unico) ---------------------------------
fuga AS (
    SELECT codunicocli AS cod_unico, MIN(mes_comprado) AS mes_fuga,
           CAST(substr(MIN(mes_comprado),1,4) AS integer)*12 + CAST(substr(MIN(mes_comprado),5,2) AS integer) AS fuga_idx,
           SUM(CAST(saldo_cancelado AS double)) AS saldo_fugado
    FROM disc_comercial.kbr_target_fuga_bn_hash
    GROUP BY codunicocli
)

-- 7) Tabla final etiquetada -------------------------------------------------
SELECT
    po.periodo, po.numeroruc, po.cod_unico,
    -- nivel: exposicion / tasa / antiguedad / cuotas / riesgo / mix
    po.saldo_total, po.n_creditos, po.monto_desem_total, po.pct_saldo_remanente,
    po.tea_max, po.tea_min, po.tea_pond, po.tea_principal,
    po.meses_desde_desem_min, po.meses_desde_desem_max,
    po.cuotas_total, po.cuotas_pend_total, po.pct_avance_cuotas, po.cuota_total,
    po.atraso_max, po.scoring_min, po.dias_venc_max,
    po.producto_principal,
    po.flg_cap_trabajo, po.flg_compra_deuda, po.flg_activo_fijo,
    po.flg_linea_revolv, po.flg_estacional, po.flg_lm_estacional,
    -- TENDENCIA (backward-looking)
    po.pct_d_saldo_3m, po.pct_d_saldo_6m, po.d_tea_pond_3m, po.d_n_creditos_3m,
    po.n_prepagos_6m, po.monto_prepago_6m, po.max_caida_rel_3m, po.meses_en_panel,
    -- referencia fuga
    f.mes_fuga, f.saldo_fugado,

    CASE WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+3 THEN 1 ELSE 0 END AS y,  -- <-- PARAM h
    CASE
        WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+3 AND f.saldo_fugado >= 0.9*po.saldo_total THEN 'FUGA_TOTAL'
        WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+3 THEN 'FUGA_PARCIAL'
        ELSE 'SE_QUEDA'
    END AS clase
FROM po
LEFT JOIN fuga f ON po.cod_unico = f.cod_unico
WHERE po.periodo_idx + 3 <= 24314           -- (a) censura derecha;  idx(202602)  <-- PARAM h
  AND po.periodo_idx + 1 >= 24302           -- (a2) piso target;     idx(202502)
  AND (f.fuga_idx IS NULL OR po.periodo_idx < f.fuga_idx)   -- (b) censura post-fuga
;
