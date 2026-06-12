-- ============================================================
-- ANALISIS DE IMPACTO DEL MODELO DE PRIORIZACION EN TIENDAS
-- ============================================================
--
-- 3 NIVELES DE ANALISIS:
--   N1: Por rangos fijos de leads P1/P2 (1-50, 51-100, ...)
--   N2: Por deciles de leads P1/P2 (10 grupos percentiles)
--   N3: Por supervisor (cod_sup)
-- ============================================================

-- ============================================
-- BASE: Datos fuente filtrados
-- ============================================
-- Limpiar tabla temporal si existe de ejecuciones previas
DROP TABLE IF EXISTS #analisis_base;

WITH base AS (
    SELECT
        PERIODO,
        EJECUTIVO,
        COD_SUP,
        SUPERVISOR,
        PRIORIZACION_CARTERA,
        NRO_LEADS,
        FLG_GESTIONADO,
        FLG_DESEMBOLSADO,
        COLOCACION AS MONTO_DESEMBOLSADO,
        FLG_ACEPTA1,
        FLG_CE
        --select distinct CAMPANHA
    FROM T_BN_FUNNEL_CANALESCOM_CLIENTE_HISTORICO
    WHERE PERIODO >= '202604'                -- Ajustar segun necesidad
        AND ID_CANAL = 1                      -- Solo Tiendas
        AND FLG_ES_CARTERA = 1                -- Cartera (donde aplica el modelo)
        AND LOWER(CAMPANHA) NOT IN ('carta fianza','garantias','giro de línea','oportunidad garantías','Recurrente Agil Estacional','Recurrente Agil Crecer')  -- Excluir garantias
        AND EJECUTIVO IS NOT NULL
        AND PRIORIZACION_CARTERA IN ('P1','P2','P3','P4')
),

-- ============================================
-- DETALLE POR EJECUTIVO (por periodo)
-- ============================================
detalle_ejecutivo AS (
    SELECT
        PERIODO,
        EJECUTIVO,
        COD_SUP,
        SUPERVISOR,

        -- Totales del ejecutivo
        SUM(NRO_LEADS) AS total_leads,
        SUM(FLG_GESTIONADO) AS total_gestionado,
        SUM(FLG_DESEMBOLSADO) AS total_desembolsos,
        SUM(MONTO_DESEMBOLSADO) AS total_monto,

        -- Solo P1/P2
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2')
            THEN NRO_LEADS ELSE 0 END) AS leads_p1p2,
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2')
            THEN FLG_GESTIONADO ELSE 0 END) AS gestionado_p1p2,
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2')
            THEN FLG_DESEMBOLSADO ELSE 0 END) AS desembolsos_p1p2,
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2')
            THEN MONTO_DESEMBOLSADO ELSE 0 END) AS monto_p1p2,

        -- P3/P4
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4')
            THEN NRO_LEADS ELSE 0 END) AS leads_p3p4,
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4')
            THEN FLG_GESTIONADO ELSE 0 END) AS gestionado_p3p4,
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4')
            THEN FLG_DESEMBOLSADO ELSE 0 END) AS desembolsos_p3p4,
        SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4')
            THEN MONTO_DESEMBOLSADO ELSE 0 END) AS monto_p3p4,

        -- ============================================
        -- INDICADORES CLAVE
        -- ============================================

        -- % GESTION P1/P2: de lo que gestiono, cuanto fue P1/P2?
        CASE WHEN SUM(FLG_GESTIONADO) > 0
            THEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_GESTIONADO ELSE 0 END)
                * 100.0 / SUM(FLG_GESTIONADO)
            ELSE NULL
        END AS pct_gestion_p1p2,

        -- % GESTION SOBRE LEADS P1/P2 (cobertura)
        CASE WHEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN NRO_LEADS ELSE 0 END) > 0
            THEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_GESTIONADO ELSE 0 END)
                * 100.0 / SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN NRO_LEADS ELSE 0 END)
            ELSE NULL
        END AS cobertura_gestion_p1p2,

        -- % COBERTURA P3/P4 (para comparar)
        CASE WHEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4') THEN NRO_LEADS ELSE 0 END) > 0
            THEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4') THEN FLG_GESTIONADO ELSE 0 END)
                * 100.0 / SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4') THEN NRO_LEADS ELSE 0 END)
            ELSE NULL
        END AS cobertura_gestion_p3p4,

        -- EFECTIVIDAD P1/P2 (desembolsos / gestionados)
        CASE WHEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_GESTIONADO ELSE 0 END) > 0
            THEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_DESEMBOLSADO ELSE 0 END)
                * 1.0 / SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_GESTIONADO ELSE 0 END)
            ELSE NULL
        END AS efectividad_p1p2,

        -- EFECTIVIDAD P3/P4 (benchmark)
        CASE WHEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4') THEN FLG_GESTIONADO ELSE 0 END) > 0
            THEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4') THEN FLG_DESEMBOLSADO ELSE 0 END)
                * 1.0 / SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P3','P4') THEN FLG_GESTIONADO ELSE 0 END)
            ELSE NULL
        END AS efectividad_p3p4,

        -- TICKET PROMEDIO P1/P2
        CASE WHEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_DESEMBOLSADO ELSE 0 END) > 0
            THEN SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN MONTO_DESEMBOLSADO ELSE 0 END)
                * 1.0 / SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN FLG_DESEMBOLSADO ELSE 0 END)
            ELSE NULL
        END AS ticket_promedio_p1p2

    FROM base
    GROUP BY PERIODO, EJECUTIVO, COD_SUP, SUPERVISOR
    HAVING SUM(CASE WHEN PRIORIZACION_CARTERA IN ('P1','P2') THEN NRO_LEADS ELSE 0 END) > 0
),

-- ============================================
-- NIVEL 1: RANGOS FIJOS DE VOLUMEN P1/P2
-- ============================================
analisis_por_rango AS (
    SELECT *,
        -- Crear rangos de volumen P1/P2
        CASE
            WHEN leads_p1p2 BETWEEN 1 AND 50 THEN '1-50'
            WHEN leads_p1p2 BETWEEN 51 AND 100 THEN '51-100'
            WHEN leads_p1p2 BETWEEN 101 AND 150 THEN '101-150'
            WHEN leads_p1p2 BETWEEN 151 AND 200 THEN '151-200'
            WHEN leads_p1p2 BETWEEN 201 AND 300 THEN '201-300'
            WHEN leads_p1p2 BETWEEN 301 AND 500 THEN '301-500'
            WHEN leads_p1p2 > 500 THEN '501+'
            ELSE 'SIN_RANGO'
        END AS rango_leads_p1p2,

        -- NTILE(2) dentro del mismo rango y periodo = ALTA/BAJA gestion
        CASE
            WHEN NTILE(2) OVER (
                PARTITION BY PERIODO,
                    CASE
                        WHEN leads_p1p2 BETWEEN 1 AND 50 THEN '1-50'
                        WHEN leads_p1p2 BETWEEN 51 AND 100 THEN '51-100'
                        WHEN leads_p1p2 BETWEEN 101 AND 150 THEN '101-150'
                        WHEN leads_p1p2 BETWEEN 151 AND 200 THEN '151-200'
                        WHEN leads_p1p2 BETWEEN 201 AND 300 THEN '201-300'
                        WHEN leads_p1p2 BETWEEN 301 AND 500 THEN '301-500'
                        WHEN leads_p1p2 > 500 THEN '501+'
                        ELSE 'SIN_RANGO'
                    END
                ORDER BY pct_gestion_p1p2 DESC
            ) = 1 THEN 'ALTA'
            ELSE 'BAJA'
        END AS tipo_gestion_rango,

        -- Contar cuantos ejecutivos hay en este rango
        COUNT(*) OVER (
            PARTITION BY PERIODO,
                CASE
                    WHEN leads_p1p2 BETWEEN 1 AND 50 THEN '1-50'
                    WHEN leads_p1p2 BETWEEN 51 AND 100 THEN '51-100'
                    WHEN leads_p1p2 BETWEEN 101 AND 150 THEN '101-150'
                    WHEN leads_p1p2 BETWEEN 151 AND 200 THEN '151-200'
                    WHEN leads_p1p2 BETWEEN 201 AND 300 THEN '201-300'
                    WHEN leads_p1p2 BETWEEN 301 AND 500 THEN '301-500'
                    WHEN leads_p1p2 > 500 THEN '501+'
                    ELSE 'SIN_RANGO'
                END
        ) AS ejecutivos_en_rango

    FROM detalle_ejecutivo
    WHERE total_gestionado > 0
      AND pct_gestion_p1p2 IS NOT NULL
),

-- ============================================
-- NIVEL 2: DECILES DE LEADS P1/P2
-- (grupos percentiles, mas justo)
-- ============================================
-- (separado en 2 CTEs porque SQL Server no permite
--  anidar funciones ventana)
-- ============================================
con_decil AS (
    SELECT *,
        -- Decil segun volumen P1/P2
        NTILE(10) OVER (
            PARTITION BY PERIODO
            ORDER BY leads_p1p2
        ) AS decil_leads_p1p2
    FROM analisis_por_rango
    WHERE ejecutivos_en_rango >= 4
),
analisis_por_decil AS (
    SELECT *,
        -- ALTA/BAJA gestion DENTRO del mismo decil
        CASE
            WHEN NTILE(2) OVER (
                PARTITION BY PERIODO, decil_leads_p1p2
                ORDER BY pct_gestion_p1p2 DESC
            ) = 1 THEN 'ALTA'
            ELSE 'BAJA'
        END AS tipo_gestion_decil
    FROM con_decil
)

-- ============================================================
-- GUARDAR BASE DE ANALISIS EN TABLA TEMPORAL
-- (para que los 3 resultados puedan consultarla)
-- ============================================================
SELECT *
INTO #analisis_base
FROM analisis_por_rango
WHERE ejecutivos_en_rango >= 4;

-- ============================================================
-- RESULTADO 1: IMPACTO POR RANGO DE VOLUMEN
-- ============================================================
-- Este es el resultado PRINCIPAL. Compara ejecutivos con volumen
-- SIMILAR de P1/P2. Si los que gestionan MAS P1/P2 obtienen
-- mejores resultados que los que gestionan MENOS, ese es el IMPACTO.
-- ============================================================
SELECT
    PERIODO,
    rango_leads_p1p2,
    ejecutivos_en_rango AS total_ejecutivos,

    -- ---- ALTA gestion P1/P2 ----
    SUM(CASE WHEN tipo_gestion_rango = 'ALTA' THEN 1 ELSE 0 END) AS ejecutivos_alta,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN pct_gestion_p1p2 ELSE NULL END), 1) AS prom_pct_gestion_alta,
    SUM(CASE WHEN tipo_gestion_rango = 'ALTA' THEN monto_p1p2 ELSE 0 END) AS monto_p1p2_alta,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN efectividad_p1p2 ELSE NULL END), 4) AS efectividad_alta,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN cobertura_gestion_p1p2 ELSE NULL END), 1) AS cobertura_alta,

    -- ---- BAJA gestion P1/P2 ----
    SUM(CASE WHEN tipo_gestion_rango = 'BAJA' THEN 1 ELSE 0 END) AS ejecutivos_baja,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN pct_gestion_p1p2 ELSE NULL END), 1) AS prom_pct_gestion_baja,
    SUM(CASE WHEN tipo_gestion_rango = 'BAJA' THEN monto_p1p2 ELSE 0 END) AS monto_p1p2_baja,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN efectividad_p1p2 ELSE NULL END), 4) AS efectividad_baja,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN cobertura_gestion_p1p2 ELSE NULL END), 1) AS cobertura_baja,

    -- === IMPACTO (ALTA - BAJA) ===
    SUM(CASE WHEN tipo_gestion_rango = 'ALTA' THEN monto_p1p2 ELSE 0 END)
        - SUM(CASE WHEN tipo_gestion_rango = 'BAJA' THEN monto_p1p2 ELSE 0 END) AS impacto_monto_absoluto,

    ROUND(
        AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN efectividad_p1p2 ELSE NULL END)
        - AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN efectividad_p1p2 ELSE NULL END)
    , 4) AS impacto_efectividad,

    ROUND(
        AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN cobertura_gestion_p1p2 ELSE NULL END)
        - AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN cobertura_gestion_p1p2 ELSE NULL END)
    , 1) AS impacto_cobertura,

    -- Ratio: cuanto mas efectivo es ALTA vs BAJA
    CASE WHEN ROUND(AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN efectividad_p1p2 ELSE NULL END), 4) > 0
        THEN ROUND(
            AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN efectividad_p1p2 ELSE NULL END)
            / AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN efectividad_p1p2 ELSE NULL END)
        , 2)
        ELSE NULL
    END AS ratio_efectividad_alta_vs_baja

FROM #analisis_base
GROUP BY PERIODO, rango_leads_p1p2, ejecutivos_en_rango
ORDER BY PERIODO DESC,
    CASE
        WHEN rango_leads_p1p2 = '1-50' THEN 1
        WHEN rango_leads_p1p2 = '51-100' THEN 2
        WHEN rango_leads_p1p2 = '101-150' THEN 3
        WHEN rango_leads_p1p2 = '151-200' THEN 4
        WHEN rango_leads_p1p2 = '201-300' THEN 5
        WHEN rango_leads_p1p2 = '301-500' THEN 6
        WHEN rango_leads_p1p2 = '501+' THEN 7
        ELSE 99
    END;

-- ============================================================
-- RESULTADO 2: RESUMEN GLOBAL (todos los rangos consolidados)
-- ============================================================
-- Este resultado da el IMPACTO TOTAL del modelo en Tiendas:
-- cuanto monto extra se genero por gestionar mas P1/P2.
-- ============================================================
SELECT
    PERIODO,
    COUNT(DISTINCT EJECUTIVO) AS total_ejecutivos_analizados,

    -- ALTA gestion
    COUNT(DISTINCT CASE WHEN tipo_gestion_rango = 'ALTA' THEN EJECUTIVO END) AS ejecutivos_alta,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN pct_gestion_p1p2 END), 1) AS avg_pct_gestion_alta,
    SUM(CASE WHEN tipo_gestion_rango = 'ALTA' THEN monto_p1p2 ELSE 0 END) AS monto_total_alta,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN efectividad_p1p2 END), 4) AS avg_efectividad_alta,

    -- BAJA gestion
    COUNT(DISTINCT CASE WHEN tipo_gestion_rango = 'BAJA' THEN EJECUTIVO END) AS ejecutivos_baja,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN pct_gestion_p1p2 END), 1) AS avg_pct_gestion_baja,
    SUM(CASE WHEN tipo_gestion_rango = 'BAJA' THEN monto_p1p2 ELSE 0 END) AS monto_total_baja,
    ROUND(AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN efectividad_p1p2 END), 4) AS avg_efectividad_baja,

    -- IMPACTO GLOBAL
    SUM(CASE WHEN tipo_gestion_rango = 'ALTA' THEN monto_p1p2 ELSE 0 END)
        - SUM(CASE WHEN tipo_gestion_rango = 'BAJA' THEN monto_p1p2 ELSE 0 END) AS impacto_monto_global,

    ROUND(
        AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN efectividad_p1p2 END)
        - AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN efectividad_p1p2 END)
    , 4) AS impacto_efectividad_global,

    ROUND(
        AVG(CASE WHEN tipo_gestion_rango = 'ALTA' THEN pct_gestion_p1p2 END)
        - AVG(CASE WHEN tipo_gestion_rango = 'BAJA' THEN pct_gestion_p1p2 END)
    , 1) AS dif_pct_gestion

FROM #analisis_base
GROUP BY PERIODO
ORDER BY PERIODO DESC;

-- ============================================================
-- RESULTADO 3: DETALLE POR SUPERVISOR
-- ============================================================
-- Quien supervisa a los ejecutivos que gestionan mas P1/P2?
-- Este resultado muestra el impacto por COD_SUP.
-- ============================================================
SELECT
    PERIODO,
    COD_SUP,
    SUPERVISOR,
    COUNT(DISTINCT EJECUTIVO) AS total_ejecutivos,
    ROUND(AVG(pct_gestion_p1p2), 1) AS avg_pct_gestion_p1p2,
    SUM(monto_p1p2) AS monto_total_p1p2,
    ROUND(AVG(efectividad_p1p2), 4) AS avg_efectividad_p1p2,
    ROUND(AVG(cobertura_gestion_p1p2), 1) AS avg_cobertura_p1p2,
    SUM(total_monto) AS monto_total_general,
    ROUND(SUM(monto_p1p2) * 100.0 / NULLIF(SUM(total_monto), 0), 1) AS pct_monto_p1p2
FROM #analisis_base
GROUP BY PERIODO, COD_SUP, SUPERVISOR
ORDER BY PERIODO DESC, SUM(monto_p1p2) DESC;
 