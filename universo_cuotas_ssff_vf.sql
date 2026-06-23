-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 1: CREAR TABLA CON PARTICIÓN POR CODMES
-- ═══════════════════════════════════════════════════════════════════════════
-- Adaptada a la nueva estructura unificada de HM_UNIVERSO_VINCULADOS_BPE:
--   - Ya no hay cuota_*_rrll ni cuota_*_conyugue por separado.
--   - Cuota del vinculado = cuota_*_vinc ; identidad = dni_hash_vinculado.
--   - cuota_sf = empresa + TODOS los vinculados (equivale al antiguo cuota_sf_2).
-- ═══════════════════════════════════════════════════════════════════════════
--  DROP TABLE disc_comercial.HM_UNIVERSO_CUOTAS_SSFF

CREATE TABLE disc_comercial.HM_UNIVERSO_CUOTAS_SSFF
WITH (
    format = 'Parquet',
    parquet_compression = 'SNAPPY',
    partitioned_by = ARRAY['codmes']
)
AS (

WITH base AS (
    SELECT
        periodo_campania,
        periodo_campania AS codmes,
        num_ruc,
        dni_hash_vinculado,
        COALESCE(cuota_negocio_empresa, 0)            AS cuota_negocio_empresa,
        COALESCE(cuota_consumo_empresa, 0)            AS cuota_consumo_empresa,
        COALESCE(cuota_prestamo_personal_empresa, 0)  AS cuota_prestamo_personal_empresa,
        COALESCE(cuota_hipotecario_empresa, 0)        AS cuota_hipotecario_empresa,
        COALESCE(cuota_total_empresa, 0)              AS cuota_total_empresa,
        COALESCE(cuota_negocio_vinc, 0)               AS cuota_negocio_vinc,
        COALESCE(cuota_consumo_vinc, 0)               AS cuota_consumo_vinc,
        COALESCE(cuota_prestamo_personal_vinc, 0)     AS cuota_prestamo_personal_vinc,
        COALESCE(cuota_hipotecario_vinc, 0)           AS cuota_hipotecario_vinc,
        COALESCE(cuota_total_vinc, 0)                 AS cuota_total_vinc
    FROM disc_comercial.HM_UNIVERSO_VINCULADOS_BPE
),

-- ── Cuota de la EMPRESA: 1 valor por RUC ──────────────────────────────────
empresa AS (
    SELECT
        periodo_campania,
        codmes,
        num_ruc,
        MAX(cuota_negocio_empresa)            AS cuota_negocio_empresa,
        MAX(cuota_consumo_empresa)            AS cuota_consumo_empresa,
        MAX(cuota_prestamo_personal_empresa)  AS cuota_prestamo_personal_empresa,
        MAX(cuota_hipotecario_empresa)        AS cuota_hipotecario_empresa,
        MAX(cuota_total_empresa)              AS cuota_total_empresa
    FROM base
    GROUP BY 1, 2, 3
),

-- ── Dedupe por vinculado (evita doble conteo si aparece en >1 vínculo) ────
vinc_rep AS (
    SELECT
        periodo_campania,
        num_ruc,
        dni_hash_vinculado,
        MAX(cuota_negocio_vinc)            AS cuota_negocio_vinc,
        MAX(cuota_consumo_vinc)            AS cuota_consumo_vinc,
        MAX(cuota_prestamo_personal_vinc)  AS cuota_prestamo_personal_vinc,
        MAX(cuota_hipotecario_vinc)        AS cuota_hipotecario_vinc,
        MAX(cuota_total_vinc)              AS cuota_total_vinc
    FROM base
    GROUP BY 1, 2, 3
),

-- ── Suma de cuotas de TODOS los vinculados por RUC ────────────────────────
vinc AS (
    SELECT
        periodo_campania,
        num_ruc,
        SUM(cuota_negocio_vinc)            AS cuota_negocio_vinc,
        SUM(cuota_consumo_vinc)            AS cuota_consumo_vinc,
        SUM(cuota_prestamo_personal_vinc)  AS cuota_prestamo_personal_vinc,
        SUM(cuota_hipotecario_vinc)        AS cuota_hipotecario_vinc,
        SUM(cuota_total_vinc)              AS cuota_total_vinc,
        COUNT(DISTINCT dni_hash_vinculado) AS nro_vinculados_unicos
    FROM vinc_rep
    GROUP BY 1, 2
)

SELECT
    e.periodo_campania,
    e.num_ruc,
    e.cuota_negocio_empresa,
    e.cuota_consumo_empresa,
    e.cuota_prestamo_personal_empresa,
    e.cuota_hipotecario_empresa,
    e.cuota_total_empresa,
    v.cuota_negocio_vinc,
    v.cuota_consumo_vinc,
    v.cuota_prestamo_personal_vinc,
    v.cuota_hipotecario_vinc,
    v.cuota_total_vinc,

    -- ── Cuota SF total: empresa + todos los vinculados ──
    COALESCE(e.cuota_total_empresa, 0) + COALESCE(v.cuota_total_vinc, 0) AS cuota_sf,

    v.nro_vinculados_unicos,
    e.codmes
FROM empresa e
LEFT JOIN vinc v
    ON e.periodo_campania = v.periodo_campania
   AND e.num_ruc = v.num_ruc
ORDER BY 1, 2
);
