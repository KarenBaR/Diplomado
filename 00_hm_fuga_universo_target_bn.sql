-- ============================================================================
-- MODELO DE FUGA - NBC Empresas  |  TABLA 1: UNIVERSO + TARGET
-- ----------------------------------------------------------------------------
-- Grano   : RUC (numeroruc) x periodo mensual
-- PO      : RUC con saldo vigente agregado > 50K en el corte t
-- Targets : y_2m = 1 si lo compro otro banco en (t, t+2];  y_3m en (t, t+3]
-- Censura : (a) derecha h=3 (tope AUTOMATICO via MAX(periodo))  (b) post-fuga
-- Piso    : 202501 (parametrizable cambiando el literal en el WHERE)
-- Solo llave + saldo_total + referencia de fuga + targets. Features -> tabla 2.
-- ============================================================================
-- DROP TABLE disc_comercial.hm_fuga_universo_target_bn

CREATE TABLE disc_comercial.hm_fuga_universo_target_bn
WITH (
    format = 'PARQUET',
    parquet_compression = 'SNAPPY',
    partitioned_by = ARRAY['periodo_ejecucion']
) AS
WITH
-- Saldo agregado por RUC x mes (define exposicion y umbral del universo) -------
saldo_mes AS (
    SELECT b.periodo, b.numeroruc, MAX(b.cod_unico) AS cod_unico,
           CAST(substr(b.periodo,1,4) AS integer)*12 + CAST(substr(b.periodo,5,2) AS integer) AS periodo_idx,
           SUM(CAST(b.saldo_pri AS double)) AS saldo_total
    FROM disc_comercial.kbr_po_fuga_bn_hash b
    WHERE b.estado_solcre='VIGENTE' AND CAST(b.saldo_pri AS double) > 0
    GROUP BY b.periodo, b.numeroruc
),
-- PO: universo con saldo > 50K en el corte t ----------------------------------
po AS (
    SELECT * FROM saldo_mes WHERE saldo_total > 50000
),

-- 6a) Detalle CREDITO x periodo desde la fuente PO (para cruzar por nro credito)
cred_mes AS (
    SELECT DISTINCT b.periodo, b.numeroruc, b.cod_credito
    FROM disc_comercial.kbr_po_fuga_bn_hash b
    WHERE b.estado_solcre='VIGENTE' AND CAST(b.saldo_pri AS double) > 0
),

-- 6b) Fuga a nivel CREDITO (target agregado por nro credito) -----------------
fuga_cred AS (
    SELECT
        credito AS cod_credito,
        MIN(mes_comprado) AS mes_fuga_cred,
        CAST(substr(MIN(mes_comprado),1,4) AS integer)*12
          + CAST(substr(MIN(mes_comprado),5,2) AS integer) AS fuga_idx_cred,
        SUM(CAST(saldo_cancelado AS double))               AS saldo_fugado_cred
    FROM disc_comercial.kbr_target_fuga_bn_hash
    GROUP BY credito
),

-- 6c) Puente CREDITO -> RUC: reagrega la fuga a RUC x periodo ----------------
--     Cruce PO x TARGET por nro credito (cod_credito = credito).
--     Solo cuentan creditos que el RUC tenia VIGENTES en el corte.
fuga AS (
    SELECT
        cm.periodo,
        cm.numeroruc,
        MIN(fc.mes_fuga_cred)     AS mes_fuga,
        MIN(fc.fuga_idx_cred)     AS fuga_idx,
        SUM(fc.saldo_fugado_cred) AS saldo_fugado
    FROM cred_mes cm
    JOIN fuga_cred fc ON cm.cod_credito = fc.cod_credito
    GROUP BY cm.periodo, cm.numeroruc
),

-- Limites del panel: tope automatico para la ventana del target ---------------
limites AS (
    SELECT MAX(CAST(substr(periodo,1,4) AS integer)*12
             + CAST(substr(periodo,5,2) AS integer)) AS idx_max
    FROM disc_comercial.kbr_po_fuga_bn_hash
    WHERE estado_solcre='VIGENTE'
)

SELECT
    po.periodo, po.numeroruc, po.cod_unico, po.saldo_total,
    f.mes_fuga, f.saldo_fugado,

    -- ===================== TARGET h=2 (proximos 2 meses) =====================
    CASE WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+2 THEN 1 ELSE 0 END AS y_2m,
    CASE
        WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+2 AND f.saldo_fugado >= 0.9*po.saldo_total THEN 'FUGA_TOTAL'
        WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+2 THEN 'FUGA_PARCIAL'
        ELSE 'SE_QUEDA'
    END AS clase_2m,

    -- ===================== TARGET h=3 (proximos 3 meses) =====================
    CASE WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+3 THEN 1 ELSE 0 END AS y_3m,
    CASE
        WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+3 AND f.saldo_fugado >= 0.9*po.saldo_total THEN 'FUGA_TOTAL'
        WHEN f.fuga_idx BETWEEN po.periodo_idx+1 AND po.periodo_idx+3 THEN 'FUGA_PARCIAL'
        ELSE 'SE_QUEDA'
    END AS clase_3m,

    SUBSTR(REPLACE(SUBSTR(CAST(date_add('month', -1, DATE_PARSE(CAST(po.periodo AS VARCHAR), '%Y%m')) AS VARCHAR), 1, 10), '-', ''), 1, 6) periodo_ejecucion
FROM po
LEFT JOIN fuga f ON po.periodo = f.periodo AND po.numeroruc = f.numeroruc
CROSS JOIN limites lim
WHERE po.periodo_idx + 3 <= lim.idx_max                       -- (a) tope AUTOMATICO: ventana h=3 observable
  AND po.periodo_idx >= CAST(substr('202501',1,4) AS integer)*12
                      + CAST(substr('202501',5,2) AS integer) -- (a2) piso = 202501 (editar solo el texto)
  AND (f.fuga_idx IS NULL OR po.periodo_idx < f.fuga_idx)     -- (b) censura post-fuga
;
