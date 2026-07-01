WITH
-- ============================================================
-- BLOQUE 1: Negocios RT con rango de ventas (3k-416k)
-- Para:
--   - nro_emp_rt, ventas_prom_rt, ventas_tot_rt
-- ============================================================
rt_ultimo_mes_en_rango AS (
    SELECT *
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY tip_doc, key_value
                ORDER BY codmes DESC
            ) AS rn
        FROM t_bpe_univ_venta_rt_datamart_version5
        WHERE codmes LIKE '2025%'
          AND prm_ventas_u12m BETWEEN 3000 AND 416000
          AND tip_doc IN ('1', '2')
    ) t
    WHERE rn = 1
),

agg_negocios_rt AS (
    SELECT
        COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo') AS ubigeo,
        COUNT(*)                            AS n_rt,
        AVG(COALESCE(d.prm_ventas_u12m, 0)) AS venta_mean_rt,
        SUM(COALESCE(d.prm_ventas_u12m, 0)) AS venta_sum_rt
    FROM rt_ultimo_mes_en_rango d
    LEFT JOIN t_bpe_sunat_vars s
        ON d.codmes = s.codmes
       AND d.tip_doc = s.tip_doc
       AND d.key_value = s.key_value
    GROUP BY COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo')
),

-- ============================================================
-- BLOQUE 2: Universo total
-- Para: n_univ_tot_transac, plin_univ_tot_transac, izipay_univ_tot_transac
-- ============================================================
universo_total AS (
    SELECT *
    FROM t_bpe_base_universo_sunat_variables
    WHERE codmes = '202512'
),

agg_universo_total AS (
    SELECT
        COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo') AS ubigeo,
        COUNT(*)                                   AS n_univ_total,
        SUM(COALESCE(pli.mto_trx_u12m, 0))         AS plin_sum_univ_total,
        SUM(COALESCE(izi.prm_mnt_izipay_u12m, 0))  AS izipay_sum_univ_total
    FROM universo_total d
    LEFT JOIN t_bpe_sunat_vars s
        ON d.codmes = s.codmes
       AND d.tip_doc = s.tip_doc
       AND d.key_value = s.key_value
    LEFT JOIN disc_datstratapy.t_bpe_brainstorn_vars_plin pli
        ON d.codmes = pli.codmes
       AND d.tip_doc = pli.tip_doc
       AND d.key_value = pli.key_value
    LEFT JOIN disc_datstratapy.t_bpe_base_izipay_v2 izi
        ON d.codmes = izi.codmes
       AND d.tip_doc = izi.tip_doc
       AND d.key_value = izi.key_value
    GROUP BY COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo')
),

-- ============================================================
-- BLOQUE 3: Normalizacion de universo accionable
-- ============================================================
universo_accionable_normalizado AS (
    SELECT
        key_value_doc AS key_value, -- el key value es el key doc
        CASE
            WHEN key_value_doc = key_value_ruc THEN '2' -- en caso haya una igualdad, significa que el key value doc fue reemplazado por key value ruc
            ELSE '1' -- y esto significa que es tip doc 2, si no son iguales, no hubo reemplazo y es tip doc 1
        END AS tip_doc,
        periodo,
        NULLIF(TRIM(clf_sf), '') AS clf_sf,

        COALESCE(deuda_sf, 0)            AS deuda_sf,
        COALESCE(deuda_sf_no_vigente, 0) AS deuda_sf_no_vigente,

        COALESCE(desembolso_ibk_con_garantia, 0)    AS desembolso_ibk_con_garantia,
        COALESCE(desembolso_no_ibk_con_garantia, 0) AS desembolso_no_ibk_con_garantia
    FROM disc_datstratapy.t_bpe_bn_universo_accionable
    WHERE periodo = '202512'
),

-- ============================================================
-- BLOQUE 4: Universo accionable
-- Para: n_univ_tot_transac_acc, plin_univ_tot_transac_acc, izipay_univ_tot_transac_acc
-- ============================================================
universo_accionable AS (
    SELECT d.*
    FROM t_bpe_base_universo_sunat_variables d
    INNER JOIN (
        SELECT DISTINCT key_value, tip_doc
        FROM universo_accionable_normalizado
    ) ua
        ON d.key_value = ua.key_value
       AND d.tip_doc   = ua.tip_doc
    WHERE d.codmes = '202512'
),

agg_universo_accionable AS (
    SELECT
        COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo') AS ubigeo,
        COUNT(*)                                   AS n_univ_accionable,
        SUM(COALESCE(pli.mto_trx_u12m, 0))         AS plin_sum_univ_accionable,
        SUM(COALESCE(izi.prm_mnt_izipay_u12m, 0))  AS izipay_sum_univ_accionable
    FROM universo_accionable d
    LEFT JOIN t_bpe_sunat_vars s
        ON d.codmes = s.codmes
       AND d.tip_doc = s.tip_doc
       AND d.key_value = s.key_value
    LEFT JOIN disc_datstratapy.t_bpe_brainstorn_vars_plin pli
        ON d.codmes = pli.codmes
       AND d.tip_doc = pli.tip_doc
       AND d.key_value = pli.key_value
    LEFT JOIN disc_datstratapy.t_bpe_base_izipay_v2 izi
        ON d.codmes = izi.codmes
       AND d.tip_doc = izi.tip_doc
       AND d.key_value = izi.key_value
    GROUP BY COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo')
),

-- ============================================================
-- BLOQUE 5: Censo CENEC MYPEs
-- ============================================================
agg_censo_mypes AS (
    SELECT
        ubigeo,
        COUNT(*)                    AS n_mypes_censo,
        SUM(ventas_corregidas_2024) AS venta_sum_mypes_censo,
        AVG(ventas_corregidas_2024) AS venta_mean_mypes_censo
    FROM censo_cenec
    WHERE ventas_corregidas_2024 >= 180000
      AND ventas_corregidas_2024 < 5000000
    GROUP BY ubigeo
),

-- ============================================================
-- BLOQUE 6: Base accionable con clasificacion y variables financieras
-- ============================================================
rt_accionable_clf_base AS (
    SELECT
        key_value,
        tip_doc,
        clf_sf,
        deuda_sf,
        deuda_sf_no_vigente,
        desembolso_ibk_con_garantia,
        desembolso_no_ibk_con_garantia
    FROM (
        SELECT
            ua.*,
            ROW_NUMBER() OVER (
                PARTITION BY ua.key_value, ua.tip_doc
                ORDER BY ua.periodo DESC
            ) AS rn
        FROM universo_accionable_normalizado ua
    ) t
    WHERE rn = 1
),

-- ============================================================
-- BLOQUE 7: RT cruzado con accionable
-- Para: nro_emp_rt_acc, ventas_prom_rt_acc, ventas_tot_rt_acc
-- ============================================================
rt_x_accionable AS (
    SELECT
        COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo') AS ubigeo,
        d.prm_ventas_u12m
    FROM rt_ultimo_mes_en_rango d
    INNER JOIN (
        SELECT DISTINCT key_value, tip_doc
        FROM rt_accionable_clf_base
    ) ab
        ON d.key_value = ab.key_value
       AND d.tip_doc   = ab.tip_doc
    LEFT JOIN t_bpe_sunat_vars s
        ON d.codmes    = s.codmes
       AND d.tip_doc   = s.tip_doc
       AND d.key_value = s.key_value
),

agg_rt_accionable AS (
    SELECT
        ubigeo,
        COUNT(*)                            AS n_rt_todos,
        AVG(COALESCE(prm_ventas_u12m, 0))   AS venta_mean_rt_todos,
        SUM(COALESCE(prm_ventas_u12m, 0))   AS venta_sum_rt_todos
    FROM rt_x_accionable
    GROUP BY ubigeo
),

-- ============================================================
-- BLOQUE 8: RT cruzado con accionable + clasificacion + variables financieras
-- ============================================================
rt_x_accionable_clf AS (
    SELECT
        COALESCE(NULLIF(s.ubigeo, ''), 'Sin Ubigeo') AS ubigeo,
        d.prm_ventas_u12m,
        ab.clf_sf,
        ab.deuda_sf,
        ab.deuda_sf_no_vigente,
        ab.desembolso_ibk_con_garantia,
        ab.desembolso_no_ibk_con_garantia
    FROM rt_ultimo_mes_en_rango d
    INNER JOIN rt_accionable_clf_base ab
        ON d.key_value = ab.key_value
       AND d.tip_doc   = ab.tip_doc
    LEFT JOIN t_bpe_sunat_vars s
        ON d.codmes    = s.codmes
       AND d.tip_doc   = s.tip_doc
       AND d.key_value = s.key_value
),

agg_rt_clf_sf_sin_imp AS (
    SELECT
        ubigeo,
        SUM(CASE WHEN clf_sf = '1.NORMAL' THEN 1 ELSE 0 END) * 1.0
            / NULLIF(SUM(CASE WHEN clf_sf IS NOT NULL THEN 1 ELSE 0 END), 0) AS pct_rt_clf_normal_clas
    FROM rt_x_accionable_clf
    GROUP BY ubigeo
),

agg_rt_penetracion_financiera AS (
    SELECT
        ubigeo,
        SUM(COALESCE(desembolso_ibk_con_garantia, 0) + COALESCE(desembolso_no_ibk_con_garantia, 0)) AS mto_desembolso_rcc_con_garantia_todos,
        SUM(COALESCE(deuda_sf, 0))            AS mto_deuda_sf,
        SUM(COALESCE(deuda_sf_no_vigente, 0)) AS mto_deuda_sf_no_vigente
    FROM rt_x_accionable_clf
    GROUP BY ubigeo
)

-- ============================================================
-- SELECT FINAL
-- ============================================================
SELECT
    base.ubigeo,
    ub.departamento,
    ub.provincia,
    ub.distrito,

    cc.n_mypes_censo          AS nro_emp_cenec_corr,
    cc.venta_sum_mypes_censo  AS ventas_tot_soles_cenec_corr,
    cc.venta_mean_mypes_censo AS ventas_prom_soles_cenec_corr,

    rt.n_rt          AS nro_emp_rt,
    rt.venta_sum_rt  AS ventas_tot_rt,
    rt.venta_mean_rt AS ventas_prom_rt,

    rta.n_rt_todos          AS nro_emp_rt_acc,
    rta.venta_sum_rt_todos  AS ventas_tot_rt_acc,
    rta.venta_mean_rt_todos AS ventas_prom_rt_acc,

    rpf.mto_desembolso_rcc_con_garantia_todos AS mto_desembolso_con_garantia_rcc,
    rclfs.pct_rt_clf_normal_clas              AS pct_rt_clf_normal_acc,
    rpf.mto_deuda_sf                          AS mto_deuda_rt_rcc,
    rpf.mto_deuda_sf_no_vigente               AS mto_deuda_no_vigente_rt_rcc,

    ut.n_univ_total          AS n_univ_tot_transac,
    ut.plin_sum_univ_total   AS plin_univ_tot_transac,
    ut.izipay_sum_univ_total AS izipay_univ_tot_transac,

    uac.n_univ_accionable          AS n_univ_tot_transac_acc,
    uac.plin_sum_univ_accionable   AS plin_univ_tot_transac_acc,
    uac.izipay_sum_univ_accionable AS izipay_univ_tot_transac_acc

FROM (
    SELECT ubigeo FROM agg_censo_mypes
    UNION
    SELECT ubigeo FROM agg_negocios_rt
    UNION
    SELECT ubigeo FROM agg_rt_accionable
    UNION
    SELECT ubigeo FROM agg_rt_clf_sf_sin_imp
    UNION
    SELECT ubigeo FROM agg_rt_penetracion_financiera
    UNION
    SELECT ubigeo FROM agg_universo_total
    UNION
    SELECT ubigeo FROM agg_universo_accionable
) base
LEFT JOIN (
    SELECT DISTINCT ubigeo_inei, departamento, provincia, distrito
    FROM t_bpe_df_ubigeos
) ub
    ON base.ubigeo = ub.ubigeo_inei
LEFT JOIN agg_censo_mypes cc
    ON base.ubigeo = cc.ubigeo
LEFT JOIN agg_negocios_rt rt
    ON base.ubigeo = rt.ubigeo
LEFT JOIN agg_rt_accionable rta
    ON base.ubigeo = rta.ubigeo
LEFT JOIN agg_rt_clf_sf_sin_imp rclfs
    ON base.ubigeo = rclfs.ubigeo
LEFT JOIN agg_rt_penetracion_financiera rpf
    ON base.ubigeo = rpf.ubigeo
LEFT JOIN agg_universo_total ut
    ON base.ubigeo = ut.ubigeo
LEFT JOIN agg_universo_accionable uac
    ON base.ubigeo = uac.ubigeo
ORDER BY base.ubigeo;