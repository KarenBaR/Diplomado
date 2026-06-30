CREATE TABLE disc_comercial.HM_UNIVERSO_VINCULADOS_BPE_v2
WITH (
    format = 'Parquet',
    parquet_compression = 'SNAPPY',
    partitioned_by = ARRAY['codmes']
)
AS (

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 1: UNIVERSO BASE (CLIENTES TIENDAS)
-- ═══════════════════════════════════════════════════════════════════════════
WITH funnel AS (
    SELECT
        numeroruc        AS num_ruc,
        periodo          AS periodo_campania,
        canal,
        campanha,
        supervisor,
        ejecutivo,
        flg_gestionado,
        flg_acepta1,
        flg_ce,
        flg_desembolsado,
        colocacion
    FROM disc_comercial.HM_FUNNEL_NEGOCIO_ACTUAL_VF f
    WHERE fecha_carga = (SELECT MAX(fecha_carga)
            FROM disc_comercial.HM_FUNNEL_NEGOCIO_ACTUAL_VF
            WHERE periodo = f.periodo)
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 2A: FUENTE PRINCIPAL → RELACIONADOS (último periodo por RUC)
-- ═══════════════════════════════════════════════════════════════════════════
relacionados_norm AS (
    SELECT
        key_value_2      AS num_ruc,
        key_value_3      AS doc_hash_vinculado,
        tip_doc2         AS tip_doc_vinculado,
        tip_vinculo,
        -- ── CATÁLOGO tip_vinculo ──
        CASE tip_vinculo
            WHEN 2  THEN 'GRUPO_ECONOMICO'   -- empresas del grupo
            WHEN 3  THEN 'REP_LEGAL'         -- representantes legales
            WHEN 4  THEN 'CONYUGE_RRLL'      -- cónyuge del RRLL
            ELSE CONCAT('VINCULO_', CAST(tip_vinculo AS VARCHAR))  -- pendiente confirmar
        END              AS origen
    FROM (
        SELECT
            *,
            MAX(p_codmes) OVER (PARTITION BY key_value_2) AS ultimo_periodo
        FROM e_perm_aws.t_fact_relacionados_rsk_v1
        -- WHERE p_codmes >= 202501   -- opcional: acota scan de particiones
    ) t
    WHERE p_codmes = ultimo_periodo
      AND tip_vinculo <> 0             -- 0 = el propio titular
),

-- RUCs que SÍ tienen relacionados (para el anti-join del fallback)
rucs_con_rel AS (
    SELECT DISTINCT num_ruc FROM relacionados_norm
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 2B: INSUMOS DEL FALLBACK (lógica anterior)
-- ═══════════════════════════════════════════════════════════════════════════
rep_legal AS (
    SELECT
        ruc_value       AS num_ruc,
        tip_doc,
        key_value       AS dni_hash_rrll
    FROM e_perm_aws.t_mst_rep_legal_sunat_rsk
    WHERE estado = '0'
),

ruc_dni AS (   -- RUC persona natural → DNI titular (1 fila por RUC)
    SELECT
        key_value_ruc_pn AS num_ruc,
        MAX(key_value)   AS dni_hash_titular
    FROM e_perm_aws.V_MST_ENCRIP_DNI_RUC_PN_HIST
    GROUP BY key_value_ruc_pn
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 3: RENIEC (género/datos + cónyuge para el fallback)
-- ═══════════════════════════════════════════════════════════════════════════
reniec AS (
    SELECT
        tip_doc,
        key_value        AS dni_hash,
        fec_nacimiento,
        est_civil,
        genero,
        tipdoc_conyugue,
        coddoc_conyugue  AS dni_hash_conyugue
    FROM e_perm_aws.t_mst_reniec_rsk
    WHERE fec_dato = (SELECT MAX(fec_dato) FROM e_perm_aws.t_mst_reniec_rsk)
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 2C: FALLBACK NORMALIZADO (solo RUC sin relacionados)
-- ═══════════════════════════════════════════════════════════════════════════
funnel_rucs AS (
    SELECT DISTINCT num_ruc FROM funnel
),

fallback_norm AS (
    -- (a) RRLL SUNAT  (o titular PN si el RUC no tiene RRLL)
    SELECT
        fr.num_ruc,
        COALESCE(rl.dni_hash_rrll, d.dni_hash_titular) AS doc_hash_vinculado,
        rl.tip_doc                                     AS tip_doc_vinculado,
        CASE WHEN rl.dni_hash_rrll IS NOT NULL THEN -1 ELSE -3 END AS tip_vinculo,
        CASE WHEN rl.dni_hash_rrll IS NOT NULL THEN 'RRLL_SUNAT' ELSE 'TITULAR_PN' END AS origen
    FROM funnel_rucs fr
    LEFT JOIN rucs_con_rel cr ON fr.num_ruc = cr.num_ruc
    LEFT JOIN rep_legal    rl ON fr.num_ruc = rl.num_ruc
    LEFT JOIN ruc_dni      d  ON fr.num_ruc = d.num_ruc
    WHERE cr.num_ruc IS NULL                                   -- anti-join
      AND COALESCE(rl.dni_hash_rrll, d.dni_hash_titular) IS NOT NULL

    UNION ALL

    -- (b) Cónyuge del RRLL (vía RENIEC)
    SELECT
        fr.num_ruc,
        rn.dni_hash_conyugue       AS doc_hash_vinculado,
        rn.tipdoc_conyugue         AS tip_doc_vinculado,
        -2                         AS tip_vinculo,
        'CONYUGE'                  AS origen
    FROM funnel_rucs fr
    LEFT JOIN rucs_con_rel cr ON fr.num_ruc = cr.num_ruc
    JOIN rep_legal rl ON fr.num_ruc = rl.num_ruc
    JOIN reniec    rn ON rl.dni_hash_rrll = rn.dni_hash AND rl.tip_doc = rn.tip_doc
    WHERE cr.num_ruc IS NULL                                   -- anti-join
      AND rn.dni_hash_conyugue IS NOT NULL
      AND rn.dni_hash_conyugue <> ''
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 2D: VINCULADOS UNIFICADOS (principal + fallback)
-- ═══════════════════════════════════════════════════════════════════════════
vinculados_base AS (
    SELECT num_ruc, doc_hash_vinculado, tip_doc_vinculado, tip_vinculo, origen
    FROM relacionados_norm
    UNION ALL
    SELECT num_ruc, doc_hash_vinculado, tip_doc_vinculado, tip_vinculo, origen
    FROM fallback_norm
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 4A: RCC PERSONAS NATURALES (DNI)
-- ═══════════════════════════════════════════════════════════════════════════
rcc_personas AS (
    SELECT
        codmes,
        tip_doc,
        key_value       AS dni_hash,
        lintot_m01,
        saltot_m01,
        salvig_m01,
        clasif_sbs_m01,
        pct_clasif_per_m01,
        pct_clasif_dud_m01,
        pct_clasif_def_m01,
        pct_clasif_cpp_m01,
        pct_clasif_nor_m01,
        saltot_m03,
        ctd_ent_tot_m01 AS cant_entidades,
        ctd_prod_m01    AS cant_productos
    FROM e_perm_aws.tbl_rcc_per_allsf_mdl
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 4B: RCC EMPRESAS (RUC)  → homologado a los mismos campos que personas
-- ═══════════════════════════════════════════════════════════════════════════
-- NOTA: qualifiqué pct_*_min / ctd_entidadreportante a la tabla A (CLASIFICACION)
--       y deuda_total_m1 a la B (SALDO). Si alguna vive en la otra tabla, reubícala.
rcc_empresa AS (
    SELECT
        a.codmes,
        a.num_ruc                  AS doc_hash_empresa,
        a.pct_normal_min,
        a.pct_cpp_min,
        a.pct_deficiente_min,
        a.pct_dudoso_min,
        a.pctperdida_min,
        b.deuda_total_m1,
        a.ctd_entidadreportante    AS cant_entidades
    FROM disc_comercial.HM_BASE_CLASIFICACION_RCC_12M a
    LEFT JOIN disc_comercial.HM_BASE_SALDO_VARIABLES_12M_CONSOLIDADA_SG b
        ON a.codmes = b.p_periodo
       AND a.num_ruc = b.num_ruc
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 5: CALCULADORA SSFF (cuota SF por documento)
-- ═══════════════════════════════════════════════════════════════════════════
calculadora_ssff AS (
    SELECT
        codmes_ejecucion,
        key_value,
        SUM(CASE WHEN producto_dsc LIKE 'Ng!_%' ESCAPE '!'
                 THEN cuota_val ELSE 0 END) AS cuota_negocio,
        SUM(CASE WHEN (producto_dsc LIKE 'C!_%' ESCAPE '!'
                       AND NOT (UPPER(producto_dsc) = 'C_LINEAUTILIZADA'
                                AND LOWER(subproducto_dsc) LIKE '%prestamo%'))
                   OR producto_dsc LIKE 'Credito consumo%'
                   OR producto_dsc = 'Saldo Credito NR-TC'
                 THEN cuota_val ELSE 0 END) AS cuota_consumo,
        SUM(CASE WHEN UPPER(producto_dsc) = 'C_LINEAUTILIZADA'
                  AND LOWER(subproducto_dsc) LIKE '%prestamo%'
                 THEN cuota_val ELSE 0 END) AS cuota_prestamo_personal,
        SUM(CASE WHEN producto_dsc = 'Credito Hipotecario'
                 THEN cuota_val ELSE 0 END) AS cuota_hipotecario,
        SUM(cuota_val) AS cuota_total
    FROM e_perm_aws.t_agg_cbpe_calculadora_ssff
    WHERE key_value IS NOT NULL AND key_value <> ''
    GROUP BY codmes_ejecucion, key_value
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 6: BASE A NIVEL VINCULADO (1 fila por vinculado del RUC)
-- ═══════════════════════════════════════════════════════════════════════════
base_vinculados AS (
    SELECT
        -- ── Datos cliente empresa ──
        f.num_ruc,
        f.periodo_campania,
        f.canal,
        f.campanha,
        f.supervisor,
        f.ejecutivo,
        f.flg_gestionado,
        f.flg_acepta1,
        f.flg_ce,
        f.flg_desembolsado,
        f.colocacion,

        -- ── Identificación del vinculado ──
        v.doc_hash_vinculado        AS dni_hash_vinculado,
        v.tip_doc_vinculado,
        v.tip_vinculo,
        v.origen                    AS vinculo_desc,

        -- Tipo de documento RCC que matcheó (persona / empresa / sin rcc)
        CASE
            WHEN rcce.doc_hash_empresa IS NOT NULL THEN 'EMPRESA'
            WHEN rccp.dni_hash         IS NOT NULL THEN 'PERSONA'
            ELSE 'SIN_RCC'
        END                         AS vinc_tipo_rcc,

        -- ── RENIEC del vinculado (solo persona) ──
        r.fec_nacimiento            AS vinc_fec_nacimiento,
        r.est_civil                 AS vinc_est_civil,
        r.genero                    AS vinc_genero,

        -- ══════════════════════════════════════════════════════════════════
        -- RCC HOMOLOGADO (persona OR empresa)
        -- ══════════════════════════════════════════════════════════════════
        rccp.lintot_m01                                     AS vinc_linea_total,       -- solo persona
        COALESCE(rccp.saltot_m01, rcce.deuda_total_m1)      AS vinc_deuda_total,
        COALESCE(rccp.salvig_m01, rcce.deuda_total_m1)      AS vinc_deuda_vigente,     -- empresa: sin vigente → usa total
        rccp.clasif_sbs_m01                                 AS vinc_calificacion,      -- solo persona
        COALESCE(rccp.pct_clasif_per_m01, rcce.pctperdida_min)     AS vinc_pct_perdida,
        COALESCE(rccp.pct_clasif_dud_m01, rcce.pct_dudoso_min)     AS vinc_pct_dudoso,
        COALESCE(rccp.pct_clasif_def_m01, rcce.pct_deficiente_min) AS vinc_pct_deficiente,
        COALESCE(rccp.pct_clasif_cpp_m01, rcce.pct_cpp_min)        AS vinc_pct_cpp,
        COALESCE(rccp.pct_clasif_nor_m01, rcce.pct_normal_min)     AS vinc_pct_normal,
        COALESCE(rccp.cant_entidades, rcce.cant_entidades)         AS vinc_cant_entidades,
        rccp.cant_productos                                 AS vinc_cant_productos,    -- solo persona
        COALESCE(rccp.saltot_m01, 0) - COALESCE(rccp.saltot_m03, 0) AS vinc_var_deuda_3m, -- solo persona

        CASE
            WHEN COALESCE(rccp.pct_clasif_per_m01, rcce.pctperdida_min, 0)     > 0 THEN 'PER'
            WHEN COALESCE(rccp.pct_clasif_dud_m01, rcce.pct_dudoso_min, 0)     > 0 THEN 'DUD'
            WHEN COALESCE(rccp.pct_clasif_def_m01, rcce.pct_deficiente_min, 0) > 0 THEN 'DEF'
            WHEN COALESCE(rccp.pct_clasif_cpp_m01, rcce.pct_cpp_min, 0)        > 0 THEN 'CPP'
            WHEN COALESCE(rccp.pct_clasif_nor_m01, rcce.pct_normal_min, 0)     > 0 THEN 'NOR'
            ELSE 'SIN_DEUDA'
        END                         AS vinc_calif_dominante,

        -- ── Cuotas SF del vinculado ──
        COALESCE(calr.cuota_negocio, 0)            AS cuota_negocio_vinc,
        COALESCE(calr.cuota_consumo, 0)            AS cuota_consumo_vinc,
        COALESCE(calr.cuota_prestamo_personal, 0)  AS cuota_prestamo_personal_vinc,
        COALESCE(calr.cuota_hipotecario, 0)        AS cuota_hipotecario_vinc,
        COALESCE(calr.cuota_total, 0)              AS cuota_total_vinc,

        -- ── Cuotas SF de la EMPRESA (igual para todas las filas del RUC) ──
        COALESCE(cale.cuota_negocio, 0)            AS cuota_negocio_empresa,
        COALESCE(cale.cuota_consumo, 0)            AS cuota_consumo_empresa,
        COALESCE(cale.cuota_prestamo_personal, 0)  AS cuota_prestamo_personal_empresa,
        COALESCE(cale.cuota_hipotecario, 0)        AS cuota_hipotecario_empresa,
        COALESCE(cale.cuota_total, 0)              AS cuota_total_empresa

    FROM funnel f

    -- Vinculados unificados (principal + fallback)
    LEFT JOIN vinculados_base v
        ON f.num_ruc = v.num_ruc

    -- RENIEC del vinculado
    LEFT JOIN reniec r
        ON v.doc_hash_vinculado = r.dni_hash
        AND v.tip_doc_vinculado = r.tip_doc

    -- RCC PERSONA (DNI) — periodo - 2 meses
    LEFT JOIN rcc_personas rccp
        ON v.doc_hash_vinculado = rccp.dni_hash
        AND v.tip_doc_vinculado = rccp.tip_doc
        AND rccp.codmes = date_format(date_add('month', -2, date_parse(f.periodo_campania, '%Y%m')), '%Y%m')

    -- RCC EMPRESA (RUC) — periodo - 2 meses
    LEFT JOIN rcc_empresa rcce
        ON v.doc_hash_vinculado = rcce.doc_hash_empresa
        AND CAST(rcce.codmes AS VARCHAR) = date_format(date_add('month', -2, date_parse(f.periodo_campania, '%Y%m')), '%Y%m')

    -- Calculadora SSFF del vinculado (periodo - 1 mes)
    LEFT JOIN calculadora_ssff calr
        ON v.doc_hash_vinculado = calr.key_value
        AND calr.codmes_ejecucion = date_format(date_add('month', -1, date_parse(f.periodo_campania, '%Y%m')), '%Y%m')

    -- Calculadora SSFF de la empresa (periodo - 1 mes)
    LEFT JOIN calculadora_ssff cale
        ON f.num_ruc = cale.key_value
        AND cale.codmes_ejecucion = date_format(date_add('month', -1, date_parse(f.periodo_campania, '%Y%m')), '%Y%m')
),

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 7: SEÑALES AGREGADAS POR EMPRESA (sobre TODOS los vinculados)
-- ═══════════════════════════════════════════════════════════════════════════
senales_vinculados AS (
    SELECT
        num_ruc,
        periodo_campania,

        MAX(CASE WHEN vinculo_desc IN ('RRLL_SUNAT','CONYUGE','TITULAR_PN')
                 THEN 'FALLBACK' ELSE 'RELACIONADOS' END) AS fuente_vinculados,

        COUNT(DISTINCT dni_hash_vinculado) AS total_vinculados,
        COUNT(DISTINCT CASE WHEN vinc_tipo_rcc = 'EMPRESA' THEN dni_hash_vinculado END) AS total_vinc_empresa,
        COUNT(DISTINCT CASE WHEN vinc_tipo_rcc = 'PERSONA' THEN dni_hash_vinculado END) AS total_vinc_persona,

        SUM(CASE WHEN COALESCE(vinc_pct_perdida, 0) > 0
                   OR COALESCE(vinc_pct_dudoso, 0) > 0
                   OR COALESCE(vinc_pct_deficiente, 0) > 0 THEN 1 ELSE 0 END) AS vinc_con_mala_calif,

        SUM(CASE WHEN COALESCE(vinc_deuda_total, 0) > 0 THEN 1 ELSE 0 END) AS vinc_con_deuda,

        SUM(CASE WHEN COALESCE(vinc_pct_cpp, 0) > 0
                   OR COALESCE(vinc_pct_deficiente, 0) > 0
                   OR COALESCE(vinc_pct_dudoso, 0) > 0
                   OR COALESCE(vinc_pct_perdida, 0) > 0 THEN 1 ELSE 0 END) AS vinc_con_calificacion_riesgosa,

        SUM(COALESCE(vinc_deuda_total, 0)) AS deuda_total_vinculados,
        SUM(COALESCE(cuota_total_vinc, 0)) AS cuota_total_vinculados,

        MAX(CASE
            WHEN COALESCE(vinc_pct_perdida, 0)    > 0 THEN 5
            WHEN COALESCE(vinc_pct_dudoso, 0)     > 0 THEN 4
            WHEN COALESCE(vinc_pct_deficiente, 0) > 0 THEN 3
            WHEN COALESCE(vinc_pct_cpp, 0)        > 0 THEN 2
            WHEN COALESCE(vinc_pct_normal, 0)     > 0 THEN 1
            ELSE 0
        END) AS peor_calificacion_score,

        MAX(CASE WHEN COALESCE(vinc_pct_cpp, 0) > 0
                   OR COALESCE(vinc_pct_deficiente, 0) > 0
                   OR COALESCE(vinc_pct_dudoso, 0) > 0
                   OR COALESCE(vinc_pct_perdida, 0) > 0 THEN 1 ELSE 0 END) AS flg_bloqueo_vinculados
    FROM base_vinculados
    GROUP BY num_ruc, periodo_campania
)

-- ═══════════════════════════════════════════════════════════════════════════
-- PASO 8: RESULTADO FINAL CON PARTICIÓN
-- ═══════════════════════════════════════════════════════════════════════════
SELECT
    b.*,
    s.fuente_vinculados,
    s.total_vinculados,
    s.total_vinc_empresa,
    s.total_vinc_persona,
    s.vinc_con_mala_calif,
    s.vinc_con_deuda,
    s.vinc_con_calificacion_riesgosa,
    s.deuda_total_vinculados,
    s.cuota_total_vinculados,
    s.peor_calificacion_score,
    s.flg_bloqueo_vinculados,

    CASE
        WHEN s.flg_bloqueo_vinculados = 1            THEN 'ALTO'
        WHEN s.vinc_con_mala_calif >= 1              THEN 'MEDIO'
        WHEN s.deuda_total_vinculados > 50000        THEN 'MEDIO'
        WHEN COALESCE(s.total_vinculados, 0) = 0     THEN 'SIN_DATOS'
        ELSE 'BAJO'
    END AS nivel_riesgo_vinculados,

    date_format(
        date_add('month', -2, date_parse(b.periodo_campania, '%Y%m')),
        '%Y%m'
    ) AS codmes

FROM base_vinculados b
LEFT JOIN senales_vinculados s
    ON b.num_ruc = s.num_ruc
   AND b.periodo_campania = s.periodo_campania
);
