"""
API 1 - CALCULO CEM (Capacidad de Endeudamiento Mensual)
Agente IA NBC Empresas - Interbank
Disenada para integracion con Copilot Studio via HTTP Actions

VERSION 3.1 - Cambios respecto a v3.0:
  - Fuente de datos: base_cem_muestra.csv (nueva BD con 60+ columnas)
  - Clave de busqueda: num_ruc (reemplaza NRO_DOC)
  - Alertas de riesgo por atraso (max_atraso_coloc_directas_ajustado + competencia)
  - Analisis de tendencia de deuda (deuda_total_var_pct_3m / 6m / 12m)
  - Clasificacion de riesgo crediticio enriquecida en buscar-ruc
  - Niveles de alerta: verde / amarillo / naranja / rojo
  - Periodo configurable en caliente via GET/POST /api/v1/config/periodo

FORMULA CEM INTERBANK (sin cambios):
  01. Ventas RT (+)              -> Input ejecutivo x factor_formalidad
  02. Costo de Ventas (-)        -> Ventas x (1 - margen_asignado)
  03. Utilidad Bruta (=)         -> Ventas - Costo
  04. Gastos Administrativos (-) -> Input ejecutivo
  05. Utilidad Operativa (=)     -> Utilidad Bruta - Gastos Admin
  06. Gastos Financieros (-)     -> Input o estimado (Deuda x 15% TEA / 12)
  07. Utilidad Neta (=)          -> Utilidad Operativa - Gastos Financieros
  08. Gastos Familiares (-)      -> S/ 10,524 anuales (S/ 877 mensuales)
  09. CEM (=)                    -> Utilidad Neta - Gastos Familiares
"""

import os
from typing import Optional
from datetime import datetime

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ================================================================
# APP
# ================================================================
app = FastAPI(
    title="API CEM - Interbank NBC Empresas",
    description="Calculo de CEM con analisis de riesgo crediticio para Copilot Studio.",
    version="3.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
# CONSTANTES
# ================================================================
TEA = 0.15
GASTOS_FAMILIARES_ANUAL = 10524.0
GASTOS_FAMILIARES_MENSUAL = round(GASTOS_FAMILIARES_ANUAL / 12, 2)

# Valor centinela para variaciones de deuda sin historia
CENTINELA_SIN_HISTORIA = -9_999_999_998.0

# Factor de informalidad segun venta mensual declarada y sector
# (venta_hasta, sector, fi, clasificacion)
# sector: "comercio", "servicio", "todos"
# Tabla FI actualizada (única por venta mensual, sin distincion por sector)
FACTOR_INFORMALIDAD_TABLA = [
    (7_500,            "todos",     6.6, "FI-01"),
    (10_000,           "todos",     5.1, "FI-02"),
    (15_000,           "todos",     4.1, "FI-03"),
    (20_000,           "todos",     3.1, "FI-04"),
    (25_000,           "todos",     2.5, "FI-05"),
    (30_000,           "todos",     2.2, "FI-06"),
    (40_000,           "todos",     2.0, "FI-07"),
    (50_000,           "todos",     1.8, "FI-08"),
    (60_000,           "todos",     1.5, "FI-09"),
    (80_000,           "todos",     1.5, "FI-10"),
    (100_000,          "todos",     1.4, "FI-11"),
    (200_000,          "todos",     1.1, "FI-12"),
    (float("inf"),     "todos",     1.0, "FI-13"),
]

# Palabras clave para inferir si el giro es servicio (resto = comercio/todos)
GIROS_SERVICIO = {
    "servicio", "consultoria", "transporte", "educacion", "salud", "logistica",
    "seguridad", "limpieza", "contabilidad", "auditoria", "tecnologia", "software",
    "mantenimiento", "reparacion", "hoteleria", "restaurante", "turismo", "agencia",
}

# Umbrales de atraso (dias) para clasificacion de riesgo
# -1 = sin colocaciones (sin historia); 0 = al dia
UMBRAL_ATRASO_BAJO    = 0    # al dia
UMBRAL_ATRASO_MEDIO   = 5    # hasta 5 dias (tolerancia operativa)
UMBRAL_ATRASO_ALTO    = 30   # hasta 30 dias
# > 30 dias = riesgo critico

# Umbrales de variacion de deuda (%) para alertas de tendencia
UMBRAL_VAR_DEUDA_CRECIMIENTO_FUERTE  =  30.0   # >30% en 3m = expansion agresiva
UMBRAL_VAR_DEUDA_CAIDA_FUERTE        = -20.0   # <-20% en 3m = posible estres
UMBRAL_VAR_DEUDA_CRECIMIENTO_SOSTENIDO = 20.0  # >20% en 12m = tendencia estructural


# ================================================================
# CARGA DE BD Y ESTADO DE PERIODO
# ================================================================
BD_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "base_cem_muestra.csv",
)

# Cache del DataFrame completo (sin filtro de periodo)
_df_raw_cache = None

# Estado global del periodo activo — se cambia via endpoint
_estado = {
    "periodo_activo": None,   # None = usa el mas reciente disponible
    "periodos_disponibles": [],
}


def _cargar_raw() -> pd.DataFrame:
    """Carga y limpia la BD completa una sola vez. Cache en memoria."""
    global _df_raw_cache
    if _df_raw_cache is not None:
        return _df_raw_cache

    try:
        df = pd.read_csv(BD_PATH, sep="|", dtype=str, encoding="utf-8")
        df.columns = [c.strip().rstrip(",") for c in df.columns]

        if "giro_canonico_match" in df.columns:
            df["giro_canonico_match"] = (
                df["giro_canonico_match"].str.rstrip(",").str.strip()
            )

        df["numeroruc"] = df["numeroruc"].astype(str).str.strip()
        df["periodo_campania"] = df["periodo_campania"].astype(str).str.strip()

        cols_numericas = [
            "deuda_total_max_12m", "cuota_sf", "margen_asignado",
            "cuota_total_rrll", "cuota_total_empresa",
            "avg_saldo_vig_prestamos_general_amt_u6m", "saldo_ajustado_ent_1",
            "venta_anual_declara_asunat_amt", "venta_anual_declara_sunat_amt",
            "ingresos_12m_sol", "ingresos_mes_sol", "total_creditos",
            "cuota_credito", "cuota_total", "meses_credito",
            "cuotas_por_pagar", "venta_anual_final", "max_cuota_credito",
            "tiempo_vida_empresa", "max_atraso_coloc_directas_ajustado",
            "max_atraso_coloc_directas_general", "max_atraso_coloc_directas_general_u6m",
            "max_atraso_ibk_coloc_directas_general",
            "max_atraso_competencia_coloc_directas_ajustado_u12m",
            "deuda_total_var_pct_3m", "deuda_total_var_pct_6m", "deuda_total_var_pct_12m",
            "deuda_noibk_var_pct_12m", "deuda_total_meses_crecimiento_12m",
            "monto_variacion_negativa_p1m_p3m", "monto_variacion_positiva_p6m_p12m",
            "monto_variacion_negativa_ult_rcc",
            "monto_variacion_negativa_p1m_p3m", "monto_variacion_positiva_p6m_p12m",
            "monto_variacion_negativa_ult_rcc",
            "cuota_sf", "cuota_total_empresa", "score_propension",
            "monto_oferta_max_ult3m", "saldo_ajustado_ent_1",
            "saldo_ajustado_ent_2", "saldo_ajustado_ent_3",
        ]
        for col in cols_numericas:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        _df_raw_cache = df

        # Poblar periodos disponibles (ordenados desc)
        periodos = sorted(df["periodo_campania"].unique().tolist(), reverse=True)
        _estado["periodos_disponibles"] = periodos

        # Si no hay periodo activo configurado, usar el mas reciente
        if _estado["periodo_activo"] is None:
            _estado["periodo_activo"] = periodos[0]
            print(f"[INFO] Periodo activo por defecto: {periodos[0]}")

        return df

    except Exception as e:
        print(f"[ERROR] No se pudo cargar BD: {e}")
        return pd.DataFrame()


def cargar_base_datos() -> pd.DataFrame:
    """Retorna el DataFrame filtrado por el periodo activo."""
    df = _cargar_raw()
    if df.empty:
        return df
    periodo = _estado["periodo_activo"]
    df_filtrado = df[df["periodo_campania"] == periodo].copy()
    df_filtrado = df_filtrado.drop_duplicates(subset="numeroruc", keep="first")
    return df_filtrado


def buscar_lead_en_bd(num_ruc: str) -> Optional[dict]:
    """Busca un RUC hasheado en el periodo activo."""
    df = cargar_base_datos()
    if df.empty:
        return None
    resultado = df[df["numeroruc"] == num_ruc]
    if resultado.empty:
        return None
    return resultado.iloc[0].to_dict()


def inferir_sector(giro: str) -> str:
    """Infiere 'servicio' o 'comercio' a partir del giro canonico del lead."""
    giro_lower = (giro or "").lower()
    for palabra in GIROS_SERVICIO:
        if palabra in giro_lower:
            return "servicio"
    return "comercio"


def obtener_factor_formalidad(venta_mensual: float, sector: str = "todos") -> tuple:
    """Retorna (factor_fi, clasificacion) segun tabla FI por venta mensual.
    La tabla actualizada no distingue por sector — aplica rango unico.
    """
    for tope, _sec, fi, clasif in FACTOR_INFORMALIDAD_TABLA:
        if venta_mensual <= tope:
            return fi, clasif
    return 1.0, "FI-13"


# ================================================================
# LOGICA DE RIESGO CREDITICIO (NUEVA)
# ================================================================

def clasificar_atraso(dias: float) -> dict:
    """
    Clasifica el nivel de riesgo segun dias de atraso.
    -1 = sin historia crediticia.
    """
    if dias < 0:
        return {"nivel": "sin_historia", "color": "gris",
                "etiqueta": "Sin historia crediticia",
                "descripcion": "El cliente no registra colocaciones en el sistema."}
    elif dias == 0:
        return {"nivel": "al_dia", "color": "verde",
                "etiqueta": "Al dia",
                "descripcion": "Sin atrasos registrados."}
    elif dias <= UMBRAL_ATRASO_MEDIO:
        return {"nivel": "atraso_leve", "color": "amarillo",
                "etiqueta": f"Atraso leve ({int(dias)} dias)",
                "descripcion": "Atraso menor a 5 dias, dentro de tolerancia operativa."}
    elif dias <= UMBRAL_ATRASO_ALTO:
        return {"nivel": "atraso_moderado", "color": "naranja",
                "etiqueta": f"Atraso moderado ({int(dias)} dias)",
                "descripcion": "Atraso entre 6 y 30 dias. Requiere evaluacion adicional."}
    else:
        return {"nivel": "atraso_critico", "color": "rojo",
                "etiqueta": f"Atraso critico ({int(dias)} dias)",
                "descripcion": "Atraso mayor a 30 dias. Alto riesgo crediticio."}


def analizar_tendencia_deuda(var_3m: float, var_6m: float, var_12m: float) -> dict:
    """
    Analiza la tendencia de endeudamiento del cliente.
    Maneja el centinela -9999999999 como 'sin historia'.
    """
    sin_historia_3m  = var_3m  <= CENTINELA_SIN_HISTORIA
    sin_historia_6m  = var_6m  <= CENTINELA_SIN_HISTORIA
    sin_historia_12m = var_12m <= CENTINELA_SIN_HISTORIA

    if sin_historia_3m and sin_historia_6m and sin_historia_12m:
        return {
            "tendencia": "sin_historia",
            "color": "gris",
            "etiqueta": "Sin historia de deuda",
            "descripcion": "No hay registro de endeudamiento previo en el sistema.",
            "var_3m": None, "var_6m": None, "var_12m": None,
        }

    # Usar valores validos; si no hay, None
    v3  = None if sin_historia_3m  else round(var_3m,  1)
    v6  = None if sin_historia_6m  else round(var_6m,  1)
    v12 = None if sin_historia_12m else round(var_12m, 1)

    # Determinar tendencia usando lo que este disponible (prioridad: 3m > 6m > 12m)
    referencia = v3 if v3 is not None else (v6 if v6 is not None else v12)

    alertas = []
    if v3 is not None and v3 >= UMBRAL_VAR_DEUDA_CRECIMIENTO_FUERTE:
        alertas.append(f"Crecimiento fuerte en 3m (+{v3}%)")
    if v3 is not None and v3 <= UMBRAL_VAR_DEUDA_CAIDA_FUERTE:
        alertas.append(f"Caida pronunciada en 3m ({v3}%)")
    if v12 is not None and v12 >= UMBRAL_VAR_DEUDA_CRECIMIENTO_SOSTENIDO:
        alertas.append(f"Tendencia creciente sostenida en 12m (+{v12}%)")

    if referencia is None:
        tendencia, color = "estable", "verde"
        descripcion = "El nivel de deuda del cliente se mantiene estable. No hay señales de alerta."
    elif referencia >= UMBRAL_VAR_DEUDA_CRECIMIENTO_FUERTE:
        tendencia, color = "expansion_agresiva", "naranja"
        v3_txt = f"+{v3}% en 3 meses" if v3 is not None else ""
        v12_txt = f", +{v12}% en 12 meses" if v12 is not None else ""
        descripcion = (
            f"La deuda total del cliente en el sistema financiero creció {v3_txt}{v12_txt}. "
            f"Está tomando financiamiento de forma acelerada y reciente. "
            f"Puede ser una empresa en expansión (oportunidad) o estar rolando deuda entre bancos (riesgo). "
            f"Cruzar con atraso y saldo para definir si conviene ofrecer ahora."
        )
    elif referencia <= UMBRAL_VAR_DEUDA_CAIDA_FUERTE:
        tendencia, color = "contraccion", "amarillo"
        v3_txt = f"{v3}% en 3 meses" if v3 is not None else ""
        descripcion = (
            f"La deuda del cliente está cayendo ({v3_txt}). "
            f"Puede estar prepagando créditos (señal positiva de liquidez) o reduciendo deuda por dificultades. "
            f"Revisar si el cliente tiene capacidad liberada para una nueva oferta."
        )
    elif referencia > 0:
        tendencia, color = "crecimiento_moderado", "amarillo"
        descripcion = (
            f"La deuda del cliente crece de forma gradual ({referencia:+.1f}% en el periodo más reciente). "
            f"Está activo en el sistema financiero y podría estar buscando más financiamiento. "
            f"Perfil normal sin señales de alerta."
        )
    else:
        tendencia, color = "estable_o_reduccion", "verde"
        descripcion = (
            f"La deuda del cliente se mantiene estable o está reduciéndose ({referencia:+.1f}%). "
            f"Sin señales de expansión agresiva ni estrés financiero visible."
        )

    return {
        "tendencia": tendencia,
        "color": color,
        "etiqueta": tendencia.replace("_", " ").title(),
        "descripcion": descripcion,
        "var_3m": v3,
        "var_6m": v6,
        "var_12m": v12,
    }


def calcular_nivel_riesgo_global(
    atraso_ibk: dict,
    atraso_competencia: dict,
    tendencia_deuda: dict,
) -> dict:
    """
    Consolida los tres factores de riesgo en un nivel global.
    Jerarquia: rojo > naranja > amarillo > verde > gris
    """
    jerarquia = {"rojo": 4, "naranja": 3, "amarillo": 2, "verde": 1, "gris": 0}

    nivel_ibk   = jerarquia.get(atraso_ibk["color"], 0)
    nivel_comp  = jerarquia.get(atraso_competencia["color"], 0)
    nivel_deuda = jerarquia.get(tendencia_deuda["color"], 0)

    max_nivel = max(nivel_ibk, nivel_comp, nivel_deuda)
    color_global = {v: k for k, v in jerarquia.items()}[max_nivel]

    etiquetas = {
        "rojo":     "RIESGO ALTO",
        "naranja":  "RIESGO MODERADO-ALTO",
        "amarillo": "RIESGO MODERADO",
        "verde":    "RIESGO BAJO",
        "gris":     "SIN HISTORIA SUFICIENTE",
    }

    factores_alerta = []
    if nivel_ibk >= 3:
        factores_alerta.append(f"Atraso IBK: {atraso_ibk['etiqueta']}")
    if nivel_comp >= 3:
        factores_alerta.append(f"Atraso competencia: {atraso_competencia['etiqueta']}")
    if nivel_deuda >= 3:
        factores_alerta.append(f"Tendencia deuda: {tendencia_deuda['etiqueta']}")

    return {
        "color": color_global,
        "etiqueta": etiquetas[color_global],
        "factores_alerta": factores_alerta,
    }


# ================================================================
# MODELOS PYDANTIC
# ================================================================

class BuscarRucRequest(BaseModel):
    ruc: str = Field(..., description="RUC real del cliente (11 digitos)")


class CalcularCEMRequest(BaseModel):
    ruc: str = Field(..., description="RUC real del cliente (11 digitos)")
    oferta_solicitada: float = Field(..., description="Monto de la oferta en S/", gt=0)
    plazo_meses: int = Field(..., description="Plazo en meses", gt=0)
    venta_mensual_rp: float = Field(..., description="Venta mensual segun RP en S/", gt=0)
    gastos_admin_mensual: float = Field(..., description="Gastos admin mensuales aprox en S/", ge=0)
    tiene_programa_pagos: bool = Field(..., description="Tiene programa de pagos financieros?")
    gasto_financiero_mensual: Optional[float] = Field(
        None, description="Gasto financiero mensual (solo si tiene_programa_pagos=True)", ge=0
    )
    gastos_familiares_mensual: Optional[float] = Field(
        None, description="Gastos familiares mensuales del titular (S/). Si no se indica, se usa el valor estándar.", ge=0
    )
    sector_override: Optional[str] = Field(
        None, description="Forzar sector: 'comercio' o 'servicio'. Si no se indica, se infiere del giro."
    )
    margen_override: Optional[float] = Field(
        None, description="Margen bruto a aplicar (0.0-1.0). Si no se indica, se usa el de la BD.", ge=0, le=1
    )


# --- Sub-modelos de respuesta ---

class AtrasoInfo(BaseModel):
    nivel: str
    color: str
    etiqueta: str
    descripcion: str


class TendenciaDeudaInfo(BaseModel):
    tendencia: str
    color: str
    etiqueta: str
    descripcion: str
    var_3m: Optional[float]
    var_6m: Optional[float]
    var_12m: Optional[float]


class RiesgoGlobalInfo(BaseModel):
    color: str
    etiqueta: str
    factores_alerta: list[str]


class AnalisisRiesgoResponse(BaseModel):
    atraso_ibk: AtrasoInfo
    atraso_competencia: AtrasoInfo
    tendencia_deuda: TendenciaDeudaInfo
    riesgo_global: RiesgoGlobalInfo


class SenialPotencial(BaseModel):
    variable: str            # nombre legible
    campo: str               # nombre técnico del campo
    valor: str               # valor del cliente formateado
    umbral: str              # umbral de referencia
    cumple: bool             # True = señal positiva
    peso: str                # "alto" / "medio" — importancia en el modelo
    descripcion: str         # insight de 1 línea


class PotencialClienteResponse(BaseModel):
    # Veredicto principal
    tiene_potencial: bool
    nivel_prioridad: str          # P1 / P2 / P3 / P4
    color_prioridad: str          # verde / azul / amarillo / rojo
    veredicto: str                # frase directa para Copilot
    score_propension: float
    score_label: str

    # Grupos del modelo
    grupo_priorizacion_tda: Optional[str] = None
    grupo_priorizacion_tv:  Optional[str] = None

    # Señales positivas (por qué SÍ tiene potencial)
    seniales_positivas: list[SenialPotencial]

    # Frenos (por qué NO tiene potencial o qué lo limita)
    frenos: list[SenialPotencial]

    # Campos de contexto
    deuda_max_12m: float
    ticket_referencia: float
    entidades_activas: float
    meses_crecimiento: float
    tiempo_vida_empresa: float
    cant_trabajadores: str

    # Mensaje ejecutivo para Copilot
    mensaje_potencial: str


class DatosLeadResponse(BaseModel):
    encontrado: bool
    ruc: str
    razon_social: Optional[str] = None
    giro_canonico: Optional[str] = None
    subsector: Optional[str] = None
    margen_asignado: Optional[float] = None
    deuda_total_sbs: Optional[float] = None
    pago_mensual_financiero: Optional[float] = None  # cuota_sf total
    cuota_total_rrll: Optional[float] = None          # suma cuota RRLL
    cuota_total_empresa: Optional[float] = None       # cuota empresa
    tiene_creditos_vigentes: Optional[bool] = None    # si tiene saldo vigente
    saldo_vigente: Optional[float] = None             # saldo vigente total
    venta_anual_sunat: Optional[float] = None         # venta_anual_declara_asunat_amt
    periodo_eeff: Optional[str] = None                # periodo EEFF de la venta SUNAT
    tiempo_vida_empresa: Optional[float] = None
    cant_trabajadores: Optional[str] = None
    ejecutivo: Optional[str] = None
    campanha: Optional[str] = None
    score_propension: Optional[float] = None
    monto_oferta_max_ult3m: Optional[float] = None
    analisis_riesgo: Optional[AnalisisRiesgoResponse] = None
    potencial: Optional[PotencialClienteResponse] = None
    # Crédito actual
    situacion_cliente: Optional[str] = None       # CON_CREDITO, EVALUADO_CAIDO
    estado_credito: Optional[str] = None          # VIGENTE, APROBADA, CANCELADA
    calificacion_cliente: Optional[str] = None    # NORMAL, PERDIDA
    producto_credito: Optional[str] = None        # CAPITAL DE TRABAJO, etc
    cuotas_por_pagar: Optional[int] = None        # cuotas pendientes
    meses_credito: Optional[int] = None           # meses del crédito actual
    total_creditos: Optional[int] = None          # total créditos activos
    cuota_total: Optional[float] = None           # cuota total mensual
    venta_anual_final: Optional[float] = None     # venta anual final
    # Bancos
    banco_principal: Optional[str] = None         # nombre banco principal
    saldo_banco_principal: Optional[float] = None # saldo banco principal
    banco_2: Optional[str] = None
    saldo_banco_2: Optional[float] = None
    # Contexto inteligente del cliente
    contexto_cliente: Optional[dict] = None
    mensaje_copilot: str


class DetalleCEMResponse(BaseModel):
    venta_mensual_rp: float
    factor_formalidad: float
    clasificacion_formalidad: str
    sector_inferido: str
    paso_01_ventas_rt: float
    paso_02_costo_ventas: float
    paso_03_utilidad_bruta: float
    paso_04_gastos_admin: float
    paso_05_utilidad_operativa: float
    paso_06_gastos_financieros: float
    paso_07_utilidad_neta: float
    paso_08_gastos_familiares: float
    paso_09_cem: float
    margen_usado: float
    fuente_gastos_financieros: str


class RecomendacionOfertaResponse(BaseModel):
    aplica: bool
    oferta_maxima_sugerida: Optional[float] = None
    cuota_oferta_max: Optional[float] = None
    cem_libre: Optional[float] = None
    deuda_max_12m: Optional[float] = None
    deuda_actual_sbs: Optional[float] = None
    headroom_historico: Optional[float] = None
    criterio: Optional[str] = None
    mensaje: str


class ResultadoCEMResponse(BaseModel):
    exito: bool
    timestamp: str
    ruc: str
    razon_social: str
    cem_mensual: float
    cuota_estimada_mensual: float
    cem_suficiente: bool
    alerta: str
    nivel_alerta: str
    recomendacion: str
    detalle: DetalleCEMResponse
    analisis_riesgo: Optional[AnalisisRiesgoResponse] = None
    recomendacion_oferta: Optional[RecomendacionOfertaResponse] = None
    mensaje_copilot: str


# ================================================================
# LOGICA CEM (sin cambios respecto a v2)
# ================================================================

def calcular_recomendacion_oferta(
    cem_mensual: float,
    cuota_actual: float,
    deuda_max_12m: float,
    deuda_actual_sbs: float,
    plazo_meses: int,
    oferta_solicitada: float,
) -> RecomendacionOfertaResponse:
    """
    Calcula si el cliente puede absorber una oferta mayor.
    Criterio conservador: mínimo entre:
      (a) Oferta máxima por CEM libre  → cem_libre / TEM → anualidad inversa
      (b) Headroom histórico            → deuda_max_12m - deuda_actual_sbs
    Solo aplica si CEM es positivo y headroom > 0.
    """
    cem_libre = round(cem_mensual - cuota_actual, 2)
    headroom  = round(deuda_max_12m - deuda_actual_sbs, 2)

    if cem_libre <= 0 or headroom <= 0 or deuda_max_12m <= 0:
        return RecomendacionOfertaResponse(
            aplica=False,
            mensaje="El cliente no tiene capacidad adicional para una oferta mayor.",
        )

    # Oferta máxima por CEM libre (anualidad inversa con TEA 15%)
    tem = (1 + TEA) ** (1 / 12) - 1
    if tem > 0 and plazo_meses > 0:
        oferta_max_cem = round(
            cem_libre * (1 - (1 + tem) ** (-plazo_meses)) / tem, 2
        )
    else:
        oferta_max_cem = round(cem_libre * plazo_meses, 2)

    # Oferta máxima sugerida = mínimo entre ambos criterios
    oferta_max_sugerida = round(min(oferta_max_cem, headroom), 2)

    # Solo recomendar si la diferencia con la oferta actual es significativa (>10%)
    if oferta_max_sugerida <= oferta_solicitada * 1.10:
        return RecomendacionOfertaResponse(
            aplica=False,
            cem_libre=cem_libre,
            deuda_max_12m=deuda_max_12m,
            deuda_actual_sbs=deuda_actual_sbs,
            headroom_historico=headroom,
            mensaje="La oferta solicitada ya está cerca del máximo recomendable.",
        )

    # Cuota para la oferta máxima sugerida
    if tem > 0 and plazo_meses > 0:
        cuota_max = round(
            oferta_max_sugerida * tem / (1 - (1 + tem) ** (-plazo_meses)), 2
        )
    else:
        cuota_max = round(oferta_max_sugerida / plazo_meses, 2)

    # Determinar criterio limitante
    criterio = (
        "headroom_historico" if headroom < oferta_max_cem else "cem_libre"
    )
    criterio_txt = (
        "Limitado por headroom histórico de deuda (deuda máx. 12m − deuda actual)"
        if criterio == "headroom_historico"
        else "Limitado por CEM libre disponible"
    )

    msg = (
        f"El cliente tiene capacidad para una oferta mayor. "
        f"CEM libre: S/ {cem_libre:,.2f}. "
        f"Headroom histórico: S/ {headroom:,.2f}. "
        f"Oferta máxima sugerida: S/ {oferta_max_sugerida:,.2f} "
        f"(cuota estimada S/ {cuota_max:,.2f} a {plazo_meses} meses)."
    )

    return RecomendacionOfertaResponse(
        aplica=True,
        oferta_maxima_sugerida=oferta_max_sugerida,
        cuota_oferta_max=cuota_max,
        cem_libre=cem_libre,
        deuda_max_12m=deuda_max_12m,
        deuda_actual_sbs=deuda_actual_sbs,
        headroom_historico=headroom,
        criterio=criterio_txt,
        mensaje=msg,
    )


def calcular_cem_formula(
    venta_mensual_rp: float,
    factor_formalidad: float,
    clasificacion: str,
    margen_asignado: float,
    gastos_admin_mensual: float,
    gasto_financiero_mensual: float,
    fuente_gf: str,
    gastos_familiares_mensual: Optional[float] = None,
) -> dict:
    """Aplica la formula CEM de Interbank (valores mensuales)."""
    ventas_rt          = venta_mensual_rp * factor_formalidad
    costo_ventas       = ventas_rt * (1 - margen_asignado)
    utilidad_bruta     = ventas_rt - costo_ventas
    utilidad_operativa = utilidad_bruta - gastos_admin_mensual
    utilidad_neta      = utilidad_operativa - gasto_financiero_mensual
    gastos_fam         = gastos_familiares_mensual if gastos_familiares_mensual is not None else GASTOS_FAMILIARES_MENSUAL
    cem                = utilidad_neta - gastos_fam

    return {
        "venta_mensual_rp":          round(venta_mensual_rp, 2),
        "factor_formalidad":         factor_formalidad,
        "clasificacion_formalidad":  clasificacion,
        "sector_inferido":           "",   # se sobreescribe en calcular_cem
        "paso_01_ventas_rt":         round(ventas_rt, 2),
        "paso_02_costo_ventas":      round(costo_ventas, 2),
        "paso_03_utilidad_bruta":    round(utilidad_bruta, 2),
        "paso_04_gastos_admin":      round(gastos_admin_mensual, 2),
        "paso_05_utilidad_operativa":round(utilidad_operativa, 2),
        "paso_06_gastos_financieros":round(gasto_financiero_mensual, 2),
        "paso_07_utilidad_neta":     round(utilidad_neta, 2),
        "paso_08_gastos_familiares": round(gastos_fam, 2),
        "paso_09_cem":               round(cem, 2),
        "margen_usado":              margen_asignado,
        "fuente_gastos_financieros": fuente_gf,
    }


def construir_analisis_riesgo(lead: dict) -> AnalisisRiesgoResponse:
    """Construye el bloque de analisis de riesgo a partir de los datos del lead."""
    atraso_ibk_dias  = float(lead.get("max_atraso_coloc_directas_ajustado", -1))
    atraso_comp_dias = float(lead.get("max_atraso_competencia_coloc_directas_ajustado_u12m", -1))
    var_3m  = float(lead.get("deuda_total_var_pct_3m",  -9_999_999_999))
    var_6m  = float(lead.get("deuda_total_var_pct_6m",  -9_999_999_999))
    var_12m = float(lead.get("deuda_total_var_pct_12m", -9_999_999_999))

    atraso_ibk_info   = clasificar_atraso(atraso_ibk_dias)
    atraso_comp_info  = clasificar_atraso(atraso_comp_dias)
    tendencia_info    = analizar_tendencia_deuda(var_3m, var_6m, var_12m)
    riesgo_global     = calcular_nivel_riesgo_global(
        atraso_ibk_info, atraso_comp_info, tendencia_info
    )

    return AnalisisRiesgoResponse(
        atraso_ibk=AtrasoInfo(**atraso_ibk_info),
        atraso_competencia=AtrasoInfo(**atraso_comp_info),
        tendencia_deuda=TendenciaDeudaInfo(**tendencia_info),
        riesgo_global=RiesgoGlobalInfo(**riesgo_global),
    )


def _riesgo_a_texto(analisis: AnalisisRiesgoResponse) -> str:
    """Convierte el analisis de riesgo a texto para mensaje_copilot."""
    lineas = [
        f"- Atraso IBK: {analisis.atraso_ibk.etiqueta}",
        f"- Atraso competencia: {analisis.atraso_competencia.etiqueta}",
    ]
    td = analisis.tendencia_deuda
    if td.var_3m is not None:
        lineas.append(f"- Variacion deuda 3m/6m/12m: {td.var_3m:+.1f}% / {td.var_6m:+.1f}% / {td.var_12m:+.1f}%")
    else:
        lineas.append("- Variacion deuda: sin historia")
    lineas.append(f"- Riesgo global: {analisis.riesgo_global.etiqueta}")
    if analisis.riesgo_global.factores_alerta:
        lineas.append("  Alertas: " + "; ".join(analisis.riesgo_global.factores_alerta))
    return "\n".join(lineas)


# ================================================================
# ENDPOINTS
# ================================================================

@app.get("/api/v1/health")
def health_check():
    """Health check para Copilot Studio."""
    df = cargar_base_datos()
    _cargar_raw()  # Asegura que periodos_disponibles este poblado
    return {
        "status":                "healthy",
        "api":                   "API-1-CEM",
        "version":               "3.1.0",
        "timestamp":             datetime.now().isoformat(),
        "bd_cargada":            not df.empty,
        "registros_periodo":     len(df) if not df.empty else 0,
        "periodo_activo":        _estado["periodo_activo"],
        "periodos_disponibles":  _estado["periodos_disponibles"],
        "fuente":                "base_cem_muestra.csv",
        "clave_busqueda":        "numeroruc",
    }


@app.get("/api/v1/config/periodo")
def ver_periodo():
    """Consulta el periodo activo y los periodos disponibles en la BD."""
    _cargar_raw()
    return {
        "periodo_activo":       _estado["periodo_activo"],
        "periodos_disponibles": _estado["periodos_disponibles"],
        "mensaje": (
            f"Periodo activo: {_estado['periodo_activo']}. "
            f"Disponibles: {', '.join(_estado['periodos_disponibles'])}."
        ),
    }


class SetPeriodoRequest(BaseModel):
    periodo: str = Field(..., description="Periodo a activar, formato YYYYMM (ej: 202604)")


@app.post("/api/v1/config/periodo")
def set_periodo(request: SetPeriodoRequest):
    """
    Cambia el periodo activo para todas las consultas siguientes.
    No requiere reiniciar la API.
    """
    _cargar_raw()
    periodo = request.periodo.strip()

    if periodo not in _estado["periodos_disponibles"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Periodo '{periodo}' no existe en la BD. "
                f"Disponibles: {', '.join(_estado['periodos_disponibles'])}"
            ),
        )

    periodo_anterior = _estado["periodo_activo"]
    _estado["periodo_activo"] = periodo

    df = cargar_base_datos()
    print(f"[INFO] Periodo cambiado: {periodo_anterior} -> {periodo} ({len(df)} registros)")

    return {
        "ok":               True,
        "periodo_anterior": periodo_anterior,
        "periodo_activo":   periodo,
        "registros":        len(df),
        "mensaje":          f"Periodo cambiado a {periodo}. {len(df)} leads disponibles.",
    }


def construir_potencial_cliente(lead: dict) -> PotencialClienteResponse:
    """
    Evalúa el potencial de adquisición de crédito del cliente.
    Basado en el Modelo de Priorización de Cartera (XGBoost, Feb 2026).

    Señales positivas (del modelo):
      1. score_propension >= 0.57 (P1/P2)
      2. deuda_total_max_12m >= 500K  → 6× más probable
      3. avg_nro_entidades_bancos_u12m >= 2  → multibanca +90%
      4. deuda_total_meses_crecimiento_12m >= 6  → ciclo expansivo

    Frenos (señales de no potencial):
      1. score_propension < 0.38  → baja propensión
      2. max_atraso_coloc_directas_ajustado > 5  → historial de atraso IBK
      3. max_atraso_competencia_coloc_directas_ajustado_u12m > 30  → atraso crítico competencia
    """
    CENT = -9_999_999_998.0

    grupo_tda = str(lead.get("grupo_propension_tda_f_v2", "P4") or "P4").strip()
    grupo_tv  = str(lead.get("grupo_propension_tv_v2",  "P4") or "P4").strip()
    score     = float(lead.get("score_propension", 0) or 0)
    deuda_max = float(lead.get("deuda_total_max_12m", 0) or 0)
    ticket    = float(lead.get("monto_oferta_max_ult3m", 0) or 0)
    entidades = float(lead.get("avg_nro_entidades_bancos_u12m", 0) or 0)
    meses_crec= float(lead.get("deuda_total_meses_crecimiento_12m", 0) or 0)
    vida      = float(lead.get("tiempo_vida_empresa", 0) or 0)
    trabajadores = str(lead.get("cant_trabajadores", "N/D") or "N/D").strip()
    atraso_ibk  = float(lead.get("max_atraso_coloc_directas_ajustado", -1) or -1)
    atraso_comp = float(lead.get("max_atraso_competencia_coloc_directas_ajustado_u12m", -1) or -1)

    # Nivel de prioridad (mejor entre TDA y TV)
    jerarquia = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
    nivel = grupo_tda if jerarquia.get(grupo_tda, 4) <= jerarquia.get(grupo_tv, 4) else grupo_tv
    color_map = {"P1": "verde", "P2": "azul", "P3": "amarillo", "P4": "rojo"}
    color = color_map.get(nivel, "gris")

    # Score label
    if score >= 0.69:   score_label = "Alto"
    elif score >= 0.57: score_label = "Medio-alto"
    elif score >= 0.38: score_label = "Medio"
    else:               score_label = "Bajo"

    # ── SEÑALES POSITIVAS ──────────────────────────────────────────
    seniales_positivas = []

    # 1. Score de propensión (variable #1 del modelo)
    seniales_positivas.append(SenialPotencial(
        variable="Score de propensión",
        campo="score_propension",
        valor=f"{score:.2f} ({score_label})",
        umbral="≥ 0.57 para P1/P2",
        cumple=score >= 0.57,
        peso="alto",
        descripcion=(
            f"Score {score:.2f} — {'alta propensión a desembolsar >100K' if score >= 0.69 else 'propensión moderada' if score >= 0.57 else 'propensión baja'}"
        ),
    ))

    # 2. Deuda máxima SF 12m (variable #2 del modelo)
    seniales_positivas.append(SenialPotencial(
        variable="Máxima deuda SF últimos 12m",
        campo="deuda_total_max_12m",
        valor=f"S/ {deuda_max:,.0f}",
        umbral="≥ S/ 500,000 (6× más probable)",
        cumple=deuda_max >= 500_000,
        peso="alto",
        descripcion=(
            f"Deuda máx S/ {deuda_max:,.0f} — {'señal fuerte: 6× más probable de desembolsar >100K' if deuda_max >= 500_000 else 'por debajo del umbral de S/ 500K del modelo'}"
        ),
    ))

    # 3. Entidades financieras activas (variable #3 del modelo)
    seniales_positivas.append(SenialPotencial(
        variable="Entidades financieras activas",
        campo="avg_nro_entidades_bancos_u12m",
        valor=f"{entidades:.1f} entidades",
        umbral="≥ 2 entidades (multibanca +90%)",
        cumple=entidades >= 2.0,
        peso="alto",
        descripcion=(
            f"{entidades:.1f} entidades — {'multibanca activo: +90% probabilidad de cierre' if entidades >= 2.0 else 'opera con menos de 2 entidades — señal débil de multibanca'}"
        ),
    ))

    # 4. Meses con deuda creciendo (ciclo expansivo)
    seniales_positivas.append(SenialPotencial(
        variable="Meses con deuda creciendo (12m)",
        campo="deuda_total_meses_crecimiento_12m",
        valor=f"{meses_crec:.0f} de 12 meses",
        umbral="≥ 6 meses (ciclo expansivo activo)",
        cumple=meses_crec >= 6,
        peso="medio",
        descripcion=(
            f"{meses_crec:.0f}/12 meses en expansión — {'ciclo expansivo activo: ventana de conversión alta' if meses_crec >= 6 else 'sin ciclo expansivo sostenido'}"
        ),
    ))

    # 5. Variación de deuda — interpretación correcta del modelo
    var_3m  = float(lead.get("deuda_total_var_pct_3m",  CENT) or CENT)
    var_6m  = float(lead.get("deuda_total_var_pct_6m",  CENT) or CENT)
    var_12m = float(lead.get("deuda_total_var_pct_12m", CENT) or CENT)
    sin_hist = var_3m <= CENT and var_6m <= CENT and var_12m <= CENT
    v3  = None if var_3m  <= CENT else round(var_3m,  1)
    v6  = None if var_6m  <= CENT else round(var_6m,  1)
    v12 = None if var_12m <= CENT else round(var_12m, 1)

    # Patrón clave: deuda alta histórica + cayendo ahora = terminando crédito, listo para otro
    deuda_alta_historica = deuda_max >= 300_000
    cayendo_reciente     = v3 is not None and v3 < -5
    crecio_largo_plazo   = v12 is not None and v12 > 0

    if sin_hist:
        var_cumple = False
        var_estado = "sin_historia"
        var_desc   = "No se registran movimientos de deuda previos en el sistema financiero. El cliente no tiene historial crediticio visible."
    elif deuda_alta_historica and cayendo_reciente:
        var_cumple = True
        var_estado = "ventana_conversion"
        partes = []
        if v3  is not None: partes.append(f"3m: {v3:+.1f}%")
        if v6  is not None: partes.append(f"6m: {v6:+.1f}%")
        if v12 is not None: partes.append(f"12m: {v12:+.1f}%")
        var_desc = (
            f"Su deuda viene bajando ({', '.join(partes)}) pero viene de un nivel alto "
            f"(S/ {deuda_max:,.0f}). Esto indica que probablemente está terminando de pagar un crédito anterior. "
            f"Es el momento ideal para presentarle una nueva oferta."
        )
    elif v3 is not None and v3 > 20:
        var_cumple = True
        var_estado = "expansion_activa"
        var_desc = (
            f"Está aumentando su deuda rápidamente ({v3:+.1f}% en los últimos 3 meses). "
            f"El cliente está en una etapa de crecimiento activo y busca más financiamiento ahora."
        )
    elif v3 is not None and v3 > 0:
        var_cumple = True
        var_estado = "crecimiento_moderado"
        var_desc = (
            f"Su deuda ha crecido moderadamente ({v3:+.1f}% en 3 meses), lo que indica que el cliente "
            f"está tomando más financiamiento de forma gradual. Buen momento para presentar una oferta."
        )
    elif v3 is not None and v3 <= -5 and not deuda_alta_historica:
        var_cumple = False
        var_estado = "contraccion"
        var_desc = (
            f"Su deuda está cayendo ({v3:+.1f}% en 3 meses) y no tiene historial de deuda alta. "
            f"Puede estar pasando por un momento de ajuste financiero. Evaluar con cautela."
        )
    else:
        var_cumple = True
        var_estado = "estable"
        partes = []
        if v3  is not None: partes.append(f"3m: {v3:+.1f}%")
        if v12 is not None: partes.append(f"12m: {v12:+.1f}%")
        var_desc = (
            f"Su nivel de deuda se mantiene estable ({', '.join(partes)}). "
            f"No hay señales de estrés ni expansión — perfil equilibrado."
        )

    var_partes_display = []
    if v3  is not None: var_partes_display.append(f"3m: {v3:+.1f}%")
    if v6  is not None: var_partes_display.append(f"6m: {v6:+.1f}%")
    if v12 is not None: var_partes_display.append(f"12m: {v12:+.1f}%")
    var_str = " · ".join(var_partes_display) if var_partes_display else "sin historia"

    seniales_positivas.append(SenialPotencial(
        variable="Variación de deuda 3m / 6m / 12m",
        campo="deuda_total_var_pct_3m/_6m/_12m",
        valor=var_str,
        umbral="Caída reciente + historial alto = ventana de conversión",
        cumple=var_cumple,
        peso="medio",
        descripcion=var_desc,
    ))

    # Señal 6: Deuda con competencia (deuda_noibk_var_pct_12m)
    noibk_var_12m = float(lead.get("deuda_noibk_var_pct_12m", 0) or 0)
    if noibk_var_12m <= CENT: noibk_var_12m = 0
    if noibk_var_12m != 0:
        if noibk_var_12m >= 20:
            noibk_cumple = True
            noibk_peso   = "alto"
            noibk_desc   = (
                f"Deuda en otras entidades creció {noibk_var_12m:+.1f}% en 12m — "
                f"el cliente está activo en financiamiento pero NO con IBK. "
                f"Interceptar antes de que consolide con competencia."
            )
        elif noibk_var_12m >= 5:
            noibk_cumple = True
            noibk_peso   = "medio"
            noibk_desc   = (
                f"Deuda con otras entidades creciendo ({noibk_var_12m:+.1f}% en 12m) — "
                f"cliente tomando financiamiento en el sistema. Evaluar oferta complementaria."
            )
        elif noibk_var_12m <= -15:
            noibk_cumple = True
            noibk_peso   = "medio"
            noibk_desc   = (
                f"Deuda en competencia cayó {noibk_var_12m:+.1f}% en 12m — "
                f"está cancelando deuda con otros bancos. Capacidad liberada para IBK."
            )
        else:
            noibk_cumple = False
            noibk_peso   = "medio"
            noibk_desc   = (
                f"Deuda en otras entidades sin movimiento significativo ({noibk_var_12m:+.1f}% en 12m)."
            )
        seniales_positivas.append(SenialPotencial(
            variable="Deuda con competencia (12m)",
            campo="deuda_noibk_var_pct_12m",
            valor=f"{noibk_var_12m:+.1f}%",
            umbral="> +20% = activo en competencia; < -15% = liberando capacidad",
            cumple=noibk_cumple,
            peso=noibk_peso,
            descripcion=noibk_desc,
        ))

    # Señal 7: Ciclo de financiamiento (monto variaciones)
    var_neg_p1m_p3m  = float(lead.get("monto_variacion_negativa_p1m_p3m",  0) or 0)
    var_pos_p6m_p12m = float(lead.get("monto_variacion_positiva_p6m_p12m", 0) or 0)
    var_neg_ult_rcc  = float(lead.get("monto_variacion_negativa_ult_rcc",   0) or 0)
    if var_neg_p1m_p3m  <= CENT: var_neg_p1m_p3m  = 0
    if var_pos_p6m_p12m <= CENT: var_pos_p6m_p12m = 0
    if var_neg_ult_rcc  <= CENT: var_neg_ult_rcc  = 0
    if any([var_neg_p1m_p3m > 0, var_pos_p6m_p12m > 0, var_neg_ult_rcc > 0]):
        pago_reciente = max(var_neg_ult_rcc, var_neg_p1m_p3m)
        pagando_ahora = pago_reciente >= 30_000
        crecio_antes  = var_pos_p6m_p12m >= 50_000
        if pagando_ahora and crecio_antes:
            ciclo_cumple = True
            ciclo_peso   = "alto"
            ciclo_desc   = (
                f"VENTANA DE CONVERSIÓN: Tomó S/ {var_pos_p6m_p12m:,.0f} de deuda hace 6–12m "
                f"y ahora está cancelando (S/ {pago_reciente:,.0f} en periodo reciente). "
                f"Está terminando un crédito — momento ideal para presentar nueva oferta."
            )
        elif pagando_ahora:
            ciclo_cumple = True
            ciclo_peso   = "medio"
            ciclo_desc   = (
                f"Canceló S/ {pago_reciente:,.0f} de deuda recientemente. "
                f"Capacidad de pago liberada — evaluar si aplica nueva oferta ahora."
            )
        elif crecio_antes:
            ciclo_cumple = deuda_max >= 200_000
            ciclo_peso   = "medio"
            ciclo_desc   = (
                f"Expandió deuda S/ +{var_pos_p6m_p12m:,.0f} en los últimos 6–12m, sin cancelación relevante aún. "
                f"{'En fase de crecimiento activo — evaluar ampliación de línea.' if deuda_max >= 200_000 else 'Monitorear: deuda creciendo sin pago reciente visible.'}"
            )
        else:
            ciclo_cumple = False
            ciclo_peso   = "medio"
            ciclo_desc   = "Movimientos de deuda pequeños, sin patrón claro de ciclo crediticio."
        ciclo_val_partes = []
        if var_pos_p6m_p12m > 0: ciclo_val_partes.append(f"+S/ {var_pos_p6m_p12m:,.0f} (6-12m)")
        if pago_reciente   > 0: ciclo_val_partes.append(f"-S/ {pago_reciente:,.0f} (reciente)")
        seniales_positivas.append(SenialPotencial(
            variable="Ciclo de financiamiento",
            campo="monto_variacion_positiva_p6m_p12m / negativa_ult_rcc / negativa_p1m_p3m",
            valor=" · ".join(ciclo_val_partes) or "sin dato",
            umbral="Creció 6-12m + paga ahora >= S/30K = ventana de conversión",
            cumple=ciclo_cumple,
            peso=ciclo_peso,
            descripcion=ciclo_desc,
        ))

    # ── FRENOS ──────────────────────────────────────────────────────
    frenos = []

    # Freno 1: Score bajo
    if score < 0.38:
        frenos.append(SenialPotencial(
            variable="Score de propensión bajo",
            campo="score_propension",
            valor=f"{score:.2f}",
            umbral="< 0.38 = baja propensión",
            cumple=False,
            peso="alto",
            descripcion=f"Score {score:.2f} por debajo del umbral mínimo — baja probabilidad de desembolso.",
        ))

    # Freno 2: Atraso IBK
    if atraso_ibk > 5:
        frenos.append(SenialPotencial(
            variable="Atraso en IBK",
            campo="max_atraso_coloc_directas_ajustado",
            valor=f"{int(atraso_ibk)} días",
            umbral="> 5 días = señal de riesgo",
            cumple=False,
            peso="alto",
            descripcion=f"Atraso {int(atraso_ibk)} días en productos IBK — limita la elegibilidad.",
        ))

    # Freno 3: Atraso en competencia
    if atraso_comp > 30:
        frenos.append(SenialPotencial(
            variable="Atraso crítico en competencia",
            campo="max_atraso_competencia_coloc_directas_ajustado_u12m",
            valor=f"{int(atraso_comp)} días",
            umbral="> 30 días = riesgo alto",
            cumple=False,
            peso="alto",
            descripcion=f"Atraso {int(atraso_comp)} días en entidades del SF — señal de estrés financiero.",
        ))

    # Freno 4: Deuda baja sin historial
    if deuda_max < 50_000 and entidades < 1:
        frenos.append(SenialPotencial(
            variable="Sin historial financiero relevante",
            campo="deuda_total_max_12m + avg_nro_entidades_bancos_u12m",
            valor=f"Deuda máx S/ {deuda_max:,.0f} · {entidades:.1f} entidades",
            umbral="< S/ 50K y < 1 entidad",
            cumple=False,
            peso="medio",
            descripcion="Sin historial crediticio relevante — perfil de bajo potencial según el modelo.",
        ))

    # ── VEREDICTO ───────────────────────────────────────────────────
    seniales_ok = sum(1 for s in seniales_positivas if s.cumple)
    frenos_criticos = sum(1 for f in frenos if f.peso == "alto")
    tiene_potencial = (nivel in ("P1", "P2")) and (frenos_criticos == 0)

    if tiene_potencial:
        if nivel == "P1":
            veredicto = (
                f"Se recomienda contactar hoy. El cliente está en la máxima prioridad de la campaña "
                f"y tiene alta probabilidad de cierre."
            )
        else:
            veredicto = (
                f"Se recomienda contactar esta semana. El potencial del cliente lo ubica en prioridad 2 "
                f"dentro de la campaña. Evalúa la oferta tomando como referencia su historial de deuda."
            )
    elif frenos_criticos > 0:
        freno_principal = frenos[0].descripcion
        veredicto = (
            f"No se recomienda presentar oferta en este momento. "
            f"{freno_principal} "
            f"Es necesario resolver esta situación antes de avanzar con el cliente."
        )
    else:
        veredicto = (
            f"El cliente no alcanza el umbral de prioridad en la campaña actual (P{nivel[-1]}). "
            f"Las señales de comportamiento financiero no son suficientemente fuertes por ahora. "
            f"Se sugiere revisarlo en el siguiente periodo o ante un cambio en su actividad crediticia."
        )

    # ── MENSAJE COPILOT ─────────────────────────────────────────────
    lineas_positivas = [f"  ✓ {s.descripcion}" for s in seniales_positivas if s.cumple]
    lineas_frenos = [f"  ✗ {f.descripcion}" for f in frenos]

    msg_parts = [f"POTENCIAL PARA CRÉDITO ({nivel} — {'CON potencial' if tiene_potencial else 'SIN potencial'}):\n"]
    msg_parts.append(f"Veredicto: {veredicto}\n")
    if lineas_positivas:
        msg_parts.append("Señales positivas:\n" + "\n".join(lineas_positivas))
    if lineas_frenos:
        msg_parts.append("\nFreno(s) identificado(s):\n" + "\n".join(lineas_frenos))

    # Conclusión ejecutiva (sin lenguaje interno del modelo)
    if tiene_potencial:
        partes_concl = []
        if deuda_max >= 300_000:
            partes_concl.append(
                f"su deuda está en proceso de liberación sobre un historial sólido de S/ {deuda_max:,.0f}"
            )
        if entidades >= 2:
            partes_concl.append(f"opera activamente con {entidades:.0f} entidades financieras")
        if not any(f.peso == "alto" for f in frenos):
            partes_concl.append("no presenta atrasos en el sistema")

        conclusion = (
            f"Este cliente tiene alto potencial para adquirir un nuevo crédito. "
            + (", ".join(partes_concl) + ". " if partes_concl else "")
        )
        if ticket > 0:
            conclusion += (
                f"Se recomienda abordar con una oferta cercana a S/ {ticket:,.0f} "
                f"antes de que otra entidad lo capture."
            )
        # Si deuda cayendo + historial alto → ventana de conversión
        if var_estado == "ventana_conversion":
            conclusion += (
                " Ventana de conversión activa — el cliente está terminando un crédito "
                "y tiene capacidad libre ahora."
            )
        msg_parts.append(f"\nRECOMENDACIÓN:\n{conclusion}")
    elif frenos_criticos > 0:
        msg_parts.append(
            f"\nRECOMENDACIÓN:\n"
            f"No se recomienda en este momento. Resolver primero: {frenos[0].descripcion}"
        )

    return PotencialClienteResponse(
        tiene_potencial=tiene_potencial,
        nivel_prioridad=nivel,
        color_prioridad=color,
        veredicto=veredicto,
        score_propension=round(score, 4),
        score_label=score_label,
        grupo_priorizacion_tda=grupo_tda,
        grupo_priorizacion_tv=grupo_tv,
        seniales_positivas=seniales_positivas,
        frenos=frenos,
        deuda_max_12m=round(deuda_max, 2),
        ticket_referencia=round(ticket, 2),
        entidades_activas=round(entidades, 2),
        meses_crecimiento=round(meses_crec, 1),
        tiempo_vida_empresa=round(vida, 1),
        cant_trabajadores=trabajadores,
        mensaje_potencial="\n".join(msg_parts),
    )



# ================================================================
# CONTEXTO INTELIGENTE DEL CLIENTE
# Aprende del historial en la BD para personalizar respuestas
# ================================================================

def construir_contexto_cliente(lead: dict) -> dict:
    """
    Analiza los campos de comportamiento del cliente en la BD
    y retorna un contexto estructurado que el agente usa para
    personalizar sus mensajes y recomendaciones.
    """
    def _int(v): 
        try: return int(float(v or 0))
        except: return 0
    def _float(v):
        try: return float(v or 0)
        except: return 0.0
    def _bool(v):
        return str(v or "0").strip() == "1"

    # ── Historial de gestión ──────────────────────────────────
    fue_gestionado   = _bool(lead.get("flg_gestionado"))
    acepto_oferta    = _bool(lead.get("flg_acepta1"))
    entro_en_ce      = _bool(lead.get("flg_ce"))
    desembolsado     = _bool(lead.get("flg_desembolsado"))
    meses_gestionado = _int(lead.get("meses_gestionados_ult3m"))
    recencia_meses   = _int(lead.get("recencia_pensara_meses"))
    periodo_ult_desembolso = str(lead.get("periodo_desembolso") or lead.get("periodo_desembolso_ult") or "").strip()
    fecha_desembolso = str(lead.get("fecha_desemb_dt") or "").strip()

    # Campos nuevos base_cem_muestra
    situacion       = str(lead.get("situacion_cliente") or "").strip()
    estado_credito  = str(lead.get("estado_credito") or "").strip()
    calificacion    = str(lead.get("calificacion_cliente_val") or "").strip().upper()
    tiene_credito   = estado_credito == "VIGENTE"
    total_creditos  = _int(lead.get("total_creditos"))
    meses_credito   = _int(lead.get("meses_credito"))
    ingresos_12m    = _float(lead.get("ingresos_12m_sol"))
    cuota_total_bd  = _float(lead.get("cuota_total"))

    # ── Comportamiento crediticio ─────────────────────────────
    cred_vigentes  = _int(lead.get("creditos_vigentes"))
    cred_vencidos  = _int(lead.get("creditos_vencidos"))
    meses_crecimiento = _float(lead.get("deuda_total_meses_crecimiento_12m"))
    atraso_ibk     = _float(lead.get("max_atraso_ibk_coloc_directas_general"))
    atraso_comp    = _float(lead.get("max_atraso_competencia_coloc_directas_ajustado_u12m"))
    cant_empresas  = _float(lead.get("cant_empresas_promedio_12m"))

    # ── Perfil de propensión ──────────────────────────────────
    grupo_tda  = str(lead.get("grupo_propension_tda_f_v2") or "").strip()
    grupo_tv   = str(lead.get("grupo_propension_tv_v2") or "").strip()
    score      = _float(lead.get("score_propension"))

    # ── Construir insights ────────────────────────────────────
    insights = []
    tono = "neutral"       # neutral | positivo | precaucion | alerta
    urgencia = "normal"    # normal | alta | baja

    # 1. Situación actual y crédito vigente (nuevos campos muestra)
    if situacion == "EN_FLUJO" or estado_credito == "VIGENTE":
        insights.append(f"tiene crédito vigente con IBK ({total_creditos} crédito{'s' if total_creditos != 1 else ''})")
        if calificacion in ("NORMAL", ""):
            tono = "positivo"
        elif calificacion == "PERDIDA":
            tono = "alerta"
    elif situacion == "CON_CREDITO":
        insights.append("tiene historial crediticio con IBK")
        tono = "positivo"
    elif situacion == "EVALUADO_CAIDO":
        insights.append("fue evaluado por Riesgos pero la operación no se concretó")
        tono = "precaucion"

    if calificacion and calificacion not in ("", "NORMAL"):
        insights.append(f"calificación crediticia IBK: {calificacion.lower()}")
        if calificacion == "PERDIDA":
            tono = "alerta"

    if meses_credito > 0:
        insights.append(f"lleva {meses_credito} {'mes' if meses_credito == 1 else 'meses'} con su crédito actual")

    # 1b. Historial de gestión previa
    if desembolsado and not situacion:
        insights.append("ya tuvo un crédito con Interbank anteriormente")
        if tono == "neutral": tono = "positivo"
    elif acepto_oferta and entro_en_ce and not situacion:
        insights.append("aceptó una oferta previa y fue evaluado por Riesgos")
        if tono == "neutral": tono = "positivo"
    elif acepto_oferta and not situacion:
        insights.append("mostró interés en una oferta anterior")
    elif fue_gestionado and not acepto_oferta and not situacion:
        if recencia_meses and recencia_meses <= 6:
            insights.append(f"fue contactado hace {recencia_meses} meses y no aceptó — puede estar en mejor momento ahora")
        elif recencia_meses and recencia_meses > 12:
            insights.append(f"no fue contactado en {recencia_meses} meses — oportunidad de retomar")
    elif not fue_gestionado and not situacion:
        insights.append("nunca ha sido gestionado — cliente sin contacto previo")
        urgencia = "alta"

    # 2. Salud crediticia
    if cred_vencidos > 0:
        insights.append(f"tiene {cred_vencidos} crédito{'s' if cred_vencidos > 1 else ''} vencido{'s' if cred_vencidos > 1 else ''} en el sistema")
        tono = "alerta"
    elif atraso_comp > 30:
        insights.append(f"ha tenido atrasos en competencia (máx {int(atraso_comp)} días)")
        tono = "precaucion"
    elif atraso_comp > 0 and atraso_comp <= 30:
        insights.append(f"atraso leve en competencia ({int(atraso_comp)} días) — dentro del rango tolerable")
    
    if atraso_ibk > 0:
        insights.append(f"registra atraso en IBK de {int(atraso_ibk)} días")
        tono = "alerta"
    elif atraso_ibk == 0 and cred_vigentes > 0:
        insights.append("al día con IBK en todos sus créditos vigentes")
        if tono == "neutral": tono = "positivo"

    # 3. Comportamiento de deuda
    if meses_crecimiento >= 8:
        insights.append(f"deuda creciendo {int(meses_crecimiento)} de 12 meses — cliente en expansión activa")
        if tono not in ("alerta", "precaucion"): tono = "positivo"
        urgencia = "alta"
    elif meses_crecimiento == 0:
        insights.append("sin crecimiento de deuda en últimos 12 meses — perfil conservador")

    # 4. Multibanca
    if cant_empresas >= 3:
        insights.append(f"opera con {cant_empresas:.0f} entidades financieras en promedio — perfil multibanca activo")
    elif cant_empresas < 1.5:
        insights.append("opera con pocas entidades financieras — potencial para ampliar relación con IBK")

    # 5. Grupo propensión
    grupo_label = {"P1": "máxima prioridad", "P2": "alta prioridad", 
                   "P3": "prioridad media", "P4": "baja prioridad"}.get(grupo_tda, "")
    if grupo_label:
        insights.append(f"clasificado como {grupo_tda} ({grupo_label}) en el modelo de propensión")

    # ── Texto para el agente ──────────────────────────────────
    if insights:
        intro = {
            "positivo":   "Lo que sé de este cliente me dice que hay una buena oportunidad",
            "precaucion": "Hay algunos puntos a tener en cuenta sobre este cliente",
            "alerta":     "Ojo con este cliente — el historial muestra señales de riesgo",
            "neutral":    "Lo que encontré sobre este cliente",
        }[tono]
        texto = intro + ":\n" + "\n".join(f"  • {i.capitalize()}." for i in insights)
    else:
        texto = "Cliente sin historial de gestión previa en el sistema."

    # ── Sugerencia de apertura para el ejecutivo ──────────────
    sugerencia = ""
    if (situacion in ("EN_FLUJO", "CON_CREDITO") or estado_credito == "VIGENTE"):
        if fecha_desembolso:
            sugerencia = f"El cliente tiene crédito activo desde {fecha_desembolso[:7]}. Enfócate en ampliar o renovar — ya confía en IBK."
        else:
            sugerencia = "Tiene crédito vigente con IBK. Ideal para oferta de ampliación o nuevo producto."
    elif situacion == "EVALUADO_CAIDO":
        sugerencia = "Fue evaluado pero no desembolsó. Averigua qué pasó — puede ser una segunda oportunidad."
    elif desembolsado and periodo_ult_desembolso:
        per = periodo_ult_desembolso
        sugerencia = f"Puedes mencionar que ya trabajaron juntos antes (desembolso en {per[:4]}) — genera confianza."
    elif acepto_oferta and not desembolsado:
        sugerencia = "Mostró interés antes pero no cerró. Enfócate en por qué esta oferta es mejor para él ahora."
    elif fue_gestionado and not acepto_oferta and recencia_meses and recencia_meses > 6:
        sugerencia = "Han pasado más de 6 meses desde el último contacto. Buen momento para abordar sin presión."
    elif tono == "alerta":
        sugerencia = "Valida el tema de créditos vencidos antes de avanzar con la oferta."
    elif urgencia == "alta" and not fue_gestionado:
        sugerencia = "Cliente sin gestión previa — primera impresión importa. Empieza por conocer su negocio."

    return {
        "tono":            tono,
        "urgencia":        urgencia,
        "insights":        insights,
        "texto_agente":    texto,
        "sugerencia":      sugerencia,
        "fue_gestionado":  fue_gestionado,
        "acepto_oferta":   acepto_oferta,
        "desembolsado":    desembolsado,
        "cred_vigentes":   cred_vigentes,
        "cred_vencidos":   cred_vencidos,
        "grupo_tda":       grupo_tda,
        "grupo_tv":        grupo_tv,
        "score":           score,
    }

@app.post("/api/v1/cem/buscar-ruc", response_model=DatosLeadResponse)
def buscar_ruc(request: BuscarRucRequest):
    """
    PASO 1: Busca el RUC real del cliente en la BD (base_cem_v5).
    Retorna datos del lead + analisis de riesgo crediticio completo.
    """
    ruc = request.ruc.strip()
    if not ruc:
        raise HTTPException(status_code=400, detail="El campo ruc no puede estar vacio.")

    lead = buscar_lead_en_bd(ruc)

    if lead is None:
        return DatosLeadResponse(
            encontrado=False,
            ruc=ruc,
            mensaje_copilot=(
                f"No se encontro un registro con RUC {ruc} en la base de datos. "
                "Verifica el identificador e intenta nuevamente."
            ),
        )

    # Extraer campos base
    giro        = str(lead.get("giro_canonico_match", "No especificado"))
    descripcion = str(lead.get("descripcion_val", ""))
    subsector   = str(lead.get("subsector_val", ""))
    margen      = float(lead.get("margen_asignado", 0.30))
    deuda       = float(lead.get("deuda_total_max_12m", 0))
    pago_fin    = float(lead.get("cuota_sf", 0))
    vida        = float(lead.get("tiempo_vida_empresa", 0))
    trabajadores= str(lead.get("cant_trabajadores", "N/D"))
    ejecutivo   = str(lead.get("ejecutivo", "N/D"))
    campanha    = str(lead.get("campanha", "N/D"))
    score       = float(lead.get("score_propension", 0))
    oferta_max  = float(lead.get("monto_oferta_max_ult3m", 0))

    # ── Campos nuevos v3.1 ──
    cuota_rrll    = float(lead.get("cuota_total_rrll", 0) or 0)
    cuota_empresa = float(lead.get("cuota_total_empresa", 0) or 0)

    # Créditos vigentes: usa avg_saldo_vig_prestamos o saldo_ajustado_ent_1
    _saldo_vig_raw = (lead.get("avg_saldo_vig_prestamos_general_amt_u6m") or
                     lead.get("saldo_ajustado_ent_1") or 0)
    saldo_vig   = float(_saldo_vig_raw or 0)
    _cred_vig   = float(lead.get("total_creditos") or lead.get("creditos_vigentes") or 0)
    _cuota_tot  = float(lead.get("cuota_total") or 0)
    tiene_creditos = saldo_vig > 0 or _cred_vig > 0 or _cuota_tot > 0

    # Ventas SUNAT con periodo EEFF
    venta_sunat   = None
    periodo_eeff  = None
    # venta_anual_final es la mejor fuente en base_cem_muestra
    _vs = (lead.get("venta_anual_final") or
           lead.get("venta_anual_declara_asunat_amt") or
           lead.get("venta_anual_declara_sunat_amt") or
           lead.get("venta_anual_eval_delclte_amt"))
    if _vs and str(_vs).strip() not in ("", "nan", "0", "0.0"):
        try:
            venta_sunat = float(_vs)
        except (ValueError, TypeError):
            venta_sunat = None
    _pe = lead.get("periodo_eeff")
    if _pe and str(_pe).strip() not in ("", "nan"):
        periodo_eeff = str(_pe).strip()

    # Analisis de riesgo crediticio
    analisis = construir_analisis_riesgo(lead)

    # Potencial del cliente (variables del modelo de priorización)
    potencial = construir_potencial_cliente(lead)

    razon_social = descripcion if descripcion else giro

    # Construir mensaje enriquecido para Copilot
    riesgo_txt = _riesgo_a_texto(analisis)

    badge_prioridad = {
        "P1": "PRIORIDAD MAXIMA",
        "P2": "PRIORIDAD ALTA",
        "P3": "PRIORIDAD MEDIA",
        "P4": "PRIORIDAD BAJA",
    }.get(potencial.nivel_prioridad, potencial.nivel_prioridad)

    # Líneas opcionales para nuevos campos
    _linea_cuotas = ""
    if cuota_rrll > 0 or cuota_empresa > 0:
        _linea_cuotas = (
            f"Cuota RRLL: S/ {cuota_rrll:,.2f} | "
            f"Cuota Empresa: S/ {cuota_empresa:,.2f}\n"
        )

    _linea_creditos = ""
    if tiene_creditos:
        _linea_creditos = f"Saldo vigente: {_fmt_miles(saldo_vig)}\n"
    else:
        _linea_creditos = "Saldo vigente: Sin créditos vigentes\n"

    _linea_sunat = ""
    if venta_sunat and venta_sunat > 0:
        _pe_txt = f" (EEFF {periodo_eeff})" if periodo_eeff else ""
        _linea_sunat = f"Venta anual SUNAT{_pe_txt}: {_fmt_miles(venta_sunat)}\n"

    # Formato cuota total desagregado
    cuota_total_sf = pago_fin + cuota_rrll + cuota_empresa
    _linea_cuotas_det = (
        f"Cuota SF total: {_fmt_miles(cuota_total_sf)} "
        f"(RRLL: {_fmt_miles(cuota_rrll)} | Empresa: {_fmt_miles(cuota_empresa)})\n"
    )

    # Humanizar nombre empresa
    nombre_display = razon_social if razon_social and razon_social != "N/D" else "el cliente"
    vida_txt = f"{vida:.0f} año{'s' if vida != 1 else ''}" if vida > 0 else "antigüedad no disponible"

    msg = (
        f"Encontré a **{nombre_display}** en la base.\n\n"
        f"📋 **Ficha del cliente**\n"
        f"• Giro: {giro}\n"
        f"• Subsector: {subsector}\n"
        f"• Antigüedad: {vida_txt}\n"
        f"• Margen asignado: {margen:.0%}\n"
        f"• Deuda máxima SBS: {_fmt_miles(deuda)}\n"
        f"{_linea_creditos}"
        f"{_linea_cuotas_det}"
        f"{_linea_sunat}"
        f"\n{potencial.mensaje_potencial}\n\n"
        f"📊 **Riesgo crediticio**\n"
        f"{riesgo_txt}\n\n"
        f"Todo listo. ¿Cuáles son las ventas promedio mensual del RT?"
    )

    # ── Nuevos campos de la BD v3 ──
    situacion    = str(lead.get("situacion_cliente") or "").strip()
    estado_cred  = str(lead.get("estado_credito") or "").strip()
    calif_cred   = str(lead.get("calificacion_cliente_val") or "").strip()
    producto_c   = str(lead.get("producto_credito") or "").strip()
    cuotas_pend  = int(float(lead.get("cuotas_por_pagar") or 0))
    meses_cred   = int(float(lead.get("meses_credito") or 0))
    total_creds  = int(float(lead.get("total_creditos") or 0))
    cuota_tot    = float(lead.get("cuota_total") or 0)
    venta_anual  = float(lead.get("venta_anual_final") or lead.get("venta_anual_declara_asunat_amt") or 0)
    banco1_nom   = str(lead.get("nomempresafinanc_desc_1") or "").strip()
    banco1_sal   = float(lead.get("saldo_ajustado_ent_1") or 0)
    banco2_nom   = str(lead.get("nomempresafinanc_desc_2") or "").strip()
    banco2_sal   = float(lead.get("saldo_ajustado_ent_2") or 0)

    # ── Construir contexto del cliente ──
    ctx = construir_contexto_cliente(lead)

    # Enriquecer mensaje_copilot con el contexto
    _ctx_txt = ""
    if ctx["texto_agente"]:
        _ctx_txt = f"\n\n🧠 **Contexto del cliente**\n{ctx['texto_agente']}"
    if ctx["sugerencia"]:
        _ctx_txt += f"\n\n💡 **Sugerencia para el ejecutivo:** {ctx['sugerencia']}"

    return DatosLeadResponse(
        encontrado=True,
        ruc=ruc,
        razon_social=razon_social,
        giro_canonico=giro,
        subsector=subsector,
        margen_asignado=margen,
        deuda_total_sbs=deuda,
        pago_mensual_financiero=pago_fin,
        cuota_total_rrll=cuota_rrll,
        cuota_total_empresa=cuota_empresa,
        tiene_creditos_vigentes=tiene_creditos,
        saldo_vigente=saldo_vig if tiene_creditos else 0.0,
        venta_anual_sunat=venta_sunat,
        periodo_eeff=periodo_eeff,
        tiempo_vida_empresa=vida,
        cant_trabajadores=trabajadores,
        ejecutivo=ejecutivo,
        campanha=campanha,
        score_propension=score,
        monto_oferta_max_ult3m=oferta_max,
        analisis_riesgo=analisis,
        potencial=potencial,
        situacion_cliente=situacion or None,
        estado_credito=estado_cred or None,
        calificacion_cliente=calif_cred or None,
        producto_credito=producto_c or None,
        cuotas_por_pagar=cuotas_pend if cuotas_pend > 0 else None,
        meses_credito=meses_cred if meses_cred > 0 else None,
        total_creditos=total_creds if total_creds > 0 else None,
        cuota_total=cuota_tot if cuota_tot > 0 else None,
        venta_anual_final=venta_anual if venta_anual > 0 else None,
        banco_principal=banco1_nom if banco1_nom and banco1_nom != 'nan' else None,
        saldo_banco_principal=banco1_sal if banco1_sal > 0 else None,
        banco_2=banco2_nom if banco2_nom and banco2_nom != 'nan' else None,
        saldo_banco_2=banco2_sal if banco2_sal > 0 else None,
        contexto_cliente=ctx,
        mensaje_copilot=msg + _ctx_txt,
    )


@app.post("/api/v1/cem/calcular", response_model=ResultadoCEMResponse)
def calcular_cem(request: CalcularCEMRequest):
    """
    PASO FINAL: Calcula el CEM con todos los inputs del ejecutivo.
    Incluye analisis de riesgo crediticio en el resultado.
    """
    # 1. Buscar lead en BD
    lead = buscar_lead_en_bd(request.ruc)

    if lead:
        descripcion  = str(lead.get("descripcion_val", ""))
        giro         = str(lead.get("giro_canonico_match", ""))
        razon_social = descripcion if descripcion else giro
        margen       = float(lead.get("margen_asignado", 0.30))
        deuda_sbs    = float(lead.get("deuda_total_max_12m", 0))
        pago_fin_bd  = float(lead.get("cuota_sf", 0))
        analisis     = construir_analisis_riesgo(lead)
    else:
        razon_social = "LEAD NO REGISTRADO"
        margen       = 0.30
        deuda_sbs    = 0
        pago_fin_bd  = 0
        analisis     = None

    # 2. Factor de formalidad (FI) segun sector inferido del giro (o override)
    if request.sector_override and request.sector_override.lower() in ("comercio", "servicio"):
        sector = request.sector_override.lower()
    else:
        sector = inferir_sector(lead.get("giro_canonico_match", "") if lead else "")
    factor, clasif = obtener_factor_formalidad(request.venta_mensual_rp, sector)

    # Margen: usar override si viene, si no el de la BD
    if request.margen_override is not None:
        margen = request.margen_override
    # (margen ya fue asignado arriba desde la BD)

    # 3. Gastos financieros
    if request.tiene_programa_pagos and request.gasto_financiero_mensual is not None:
        gasto_fin = request.gasto_financiero_mensual
        fuente_gf = "programa_pagos"
    else:
        if pago_fin_bd > 0:
            gasto_fin = pago_fin_bd
            fuente_gf = "estimado_bd"
        else:
            gasto_fin = round(deuda_sbs * TEA / 12, 2)
            fuente_gf = "estimado_tea"

    # 4. Calcular CEM
    detalle = calcular_cem_formula(
        venta_mensual_rp=request.venta_mensual_rp,
        factor_formalidad=factor,
        clasificacion=clasif,
        margen_asignado=margen,
        gastos_admin_mensual=request.gastos_admin_mensual,
        gasto_financiero_mensual=gasto_fin,
        fuente_gf=fuente_gf,
        gastos_familiares_mensual=request.gastos_familiares_mensual,
    )
    detalle["sector_inferido"] = sector   # sobreescribir con el valor real
    cem = detalle["paso_09_cem"]

    # 5. Cuota estimada (anualidad con TEM)
    tem = (1 + TEA) ** (1 / 12) - 1
    if tem > 0 and request.plazo_meses > 0:
        cuota = round(
            request.oferta_solicitada * tem / (1 - (1 + tem) ** (-request.plazo_meses)), 2
        )
    else:
        cuota = round(request.oferta_solicitada / request.plazo_meses, 2)

    # 6. Evaluar CEM
    cem_ok = cem >= cuota

    # Prefijo de riesgo para mensaje (si hay analisis)
    prefijo_riesgo = ""
    if analisis and analisis.riesgo_global.color in ("rojo", "naranja"):
        prefijo_riesgo = (
            f"\n⚠ ALERTA DE RIESGO: {analisis.riesgo_global.etiqueta}\n"
            + ("\n".join(f"  - {f}" for f in analisis.riesgo_global.factores_alerta))
            + "\n"
        )

    if cem < 0:
        alerta, nivel = "CEM_NEGATIVO", "rojo"
        recom = (
            f"CEM negativo (S/ {cem:,.2f}). No se recomienda la oferta. "
            f"Revisar estructura de costos o reducir oferta."
        )
        msg = (
            f"ALERTA: CEM NEGATIVO{prefijo_riesgo}\n"
            f"Para {razon_social} (RUC: {request.ruc}):\n"
            f"- Oferta: S/ {request.oferta_solicitada:,.2f} a {request.plazo_meses} meses\n"
            f"- CEM: S/ {cem:,.2f}\n"
            f"- Factor formalidad: {factor:.0%} (Clasif. {clasif})\n\n"
            f"El CEM es negativo. Se sugiere:\n"
            f"1. Revisar ventas declaradas\n"
            f"2. Verificar gastos administrativos\n"
            f"3. Bajar la oferta significativamente"
        )
    elif not cem_ok:
        alerta, nivel = "CEM_AJUSTADO", "amarillo"
        if tem > 0:
            oferta_max = max(0, round(cem * (1 - (1 + tem) ** (-request.plazo_meses)) / tem, 2))
        else:
            oferta_max = max(0, round(cem * request.plazo_meses, 2))
        recom = (
            f"CEM (S/ {cem:,.2f}) no cubre cuota (S/ {cuota:,.2f}). "
            f"Oferta maxima viable: S/ {oferta_max:,.2f}."
        )
        msg = (
            f"❌ **CEM insuficiente**{prefijo_riesgo}\n"
            f"Para **{razon_social}**:\n"
            f"• Oferta solicitada: S/ {request.oferta_solicitada:,.2f} · {request.plazo_meses} meses\n"
            f"• Cuota estimada: S/ {cuota:,.2f}\n"
            f"• CEM calculado: S/ {cem:,.2f}\n"
            f"• Factor de formalidad: {factor:.1f}x ({clasif})\n\n"
            f"El CEM no cubre la cuota. Se sugiere ajustar la oferta a máximo **S/ {oferta_max:,.2f}**."
        )
    else:
        # CEM positivo — verificar si hay riesgo crediticio que condicione
        if analisis and analisis.riesgo_global.color == "rojo":
            alerta, nivel = "CEM_POSITIVO_RIESGO_ALTO", "naranja"
            recom = (
                f"CEM (S/ {cem:,.2f}) cubre cuota (S/ {cuota:,.2f}), "
                f"pero el cliente presenta riesgo crediticio alto. Evaluar con precaucion."
            )
        else:
            alerta, nivel = "CEM_POSITIVO", "verde"
            recom = (
                f"CEM (S/ {cem:,.2f}) cubre cuota (S/ {cuota:,.2f}). Cliente califica."
            )
        msg = (
            f"✅ **CEM aprobado**{prefijo_riesgo}\n"
            f"**{razon_social}** califica para la oferta.\n"
            f"• Oferta: S/ {request.oferta_solicitada:,.2f} · {request.plazo_meses} meses\n"
            f"• Cuota estimada: S/ {cuota:,.2f}\n"
            f"• CEM calculado: S/ {cem:,.2f}\n"
            f"• Factor de formalidad: {factor:.1f}x ({clasif})\n\n"
            f"¿Quieres revisar el potencial del cliente o sus vinculados?"
        )

    # 7. Recomendación de oferta mayor (solo si CEM positivo)
    rec_oferta = None
    # Contexto del cliente para personalizar el cierre del cálculo
    ctx_calc = construir_contexto_cliente(lead) if lead else {}
    _ctx_calcular = ""
    if ctx_calc.get("sugerencia"):
        _ctx_calcular = f"\n\n💡 **Sugerencia:** {ctx_calc['sugerencia']}"
    if ctx_calc.get("tono") == "alerta" and ctx_calc.get("cred_vencidos", 0) > 0:
        _ctx_calcular += f"\n⚠️ Recuerda que este cliente tiene {ctx_calc['cred_vencidos']} crédito(s) vencido(s) — considera esto antes de enviar a Riesgos."

    if cem_ok and lead:
        deuda_max_12m   = float(lead.get("deuda_total_max_12m", 0))
        deuda_actual    = float(lead.get("deuda_total_max_12m", 0))  # fallback
        rec_oferta = calcular_recomendacion_oferta(
            cem_mensual=cem,
            cuota_actual=cuota,
            deuda_max_12m=deuda_max_12m,
            deuda_actual_sbs=deuda_sbs if lead else 0,
            plazo_meses=request.plazo_meses,
            oferta_solicitada=request.oferta_solicitada,
        )
        # Enriquecer mensaje si aplica recomendación
        if rec_oferta and rec_oferta.aplica:
            msg += (
                f"\n\n💡 RECOMENDACIÓN: El cliente puede absorber una oferta mayor.\n"
                f"- CEM libre: S/ {rec_oferta.cem_libre:,.2f}\n"
                f"- Headroom histórico (deuda máx. 12m): S/ {rec_oferta.headroom_historico:,.2f}\n"
                f"- Oferta máxima sugerida: S/ {rec_oferta.oferta_maxima_sugerida:,.2f} "
                f"(cuota S/ {rec_oferta.cuota_oferta_max:,.2f})\n"
                f"- Criterio: {rec_oferta.criterio}"
            )

    return ResultadoCEMResponse(
        exito=True,
        timestamp=datetime.now().isoformat(),
        ruc=request.ruc,
        razon_social=razon_social,
        cem_mensual=cem,
        cuota_estimada_mensual=cuota,
        cem_suficiente=cem_ok,
        alerta=alerta,
        nivel_alerta=nivel,
        recomendacion=recom,
        detalle=DetalleCEMResponse(**detalle),
        analisis_riesgo=analisis,
        recomendacion_oferta=rec_oferta,
        mensaje_copilot=msg + _ctx_calcular,
    )


# ================================================================
# ARRANQUE
# ================================================================
if __name__ == "__main__":
    print("=" * 55)
    print("  API CEM v3.1 - Interbank NBC Empresas")
    print("  BD: base_cem_v5.csv")
    print(f"  Swagger: http://localhost:8000/docs")
    print("=" * 55)
    uvicorn.run("Simulacion3:app", host="0.0.0.0", port=8000, reload=True)


# ================================================================
# VINCULADOS — Modelos y Lógica
# ================================================================

BD_VINCULADOS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "base_vinculados_2026.csv",
)

_df_vinculados_cache = None

CALIF_LABEL = {
    "NOR": "Normal", "CPP": "CPP", "DEF": "Deficiente",
    "DUD": "Dudoso",  "PER": "Pérdida", "SIN_DEUDA": "Sin deuda",
}
CALIF_COLOR = {
    "NOR": "verde",  "CPP": "amarillo", "DEF": "naranja",
    "DUD": "naranja","PER": "rojo",     "SIN_DEUDA": "gris",
}

def cargar_vinculados() -> pd.DataFrame:
    global _df_vinculados_cache
    if _df_vinculados_cache is not None:
        return _df_vinculados_cache
    try:
        df = pd.read_csv(BD_VINCULADOS_PATH, sep="|", dtype=str, encoding="latin-1")
        df.columns = [c.strip().rstrip(",") for c in df.columns]
        df["numeroruc"] = df["numeroruc"].str.strip()
        # Convertir numéricos clave
        num_cols = [
            "rrll_deuda_total","rrll_deuda_vigente","rrll_linea_total",
            "rrll_calificacion","rrll_cant_entidades","rrll_cant_productos",
            "rrll_var_deuda_3m","rrll_pct_normal","rrll_pct_cpp",
            "rrll_pct_deficiente","rrll_pct_dudoso","rrll_pct_perdida",
            "conyugue_deuda_total","conyugue_deuda_vigente","conyugue_cant_entidades",
            "conyugue_pct_normal","conyugue_pct_cpp","conyugue_pct_deficiente",
            "conyugue_pct_dudoso","conyugue_pct_perdida",
            "cuota_total_rrll","cuota_total_conyugue","cuota_total_empresa",
            "deuda_total_rrll","deuda_total_conyugue","deuda_total_vinculados",
            "total_rrll","rrll_con_mala_calif","rrll_con_deuda",
            "rrll_con_calificacion_riesgosa","conyugue_con_mala_calif",
            "peor_calificacion_score",
        ]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df = df.sort_values("periodo_campania", ascending=False)
        df = df.drop_duplicates(subset="numeroruc", keep="first")
        _df_vinculados_cache = df
        print(f"[INFO] Vinculados cargados: {len(df)} RUCs únicos")
        return df
    except Exception as e:
        print(f"[ERROR] No se pudo cargar vinculados: {e}")
        return pd.DataFrame()


def _fmt_miles(n: float) -> str:
    """Formato compacto en miles: S/ 500K, S/ 1.2M"""
    if not n:
        return "S/ 0"
    if abs(n) >= 1_000_000:
        return f"S/ {n/1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"S/ {n/1_000:.0f}K"
    return f"S/ {n:,.0f}"


class BancoInfo(BaseModel):
    nombre: str
    saldo: float
    participacion_pct: Optional[float] = None
    atraso_dias: Optional[int] = None
    tendencia: Optional[str] = None

class RRLLInfo(BaseModel):
    numero: int                    # 1, 2, 3...
    dni: str
    cargo: str
    estado_civil: str
    calificacion_dominante: str
    calificacion_label: str
    calificacion_color: str
    deuda_total: float
    deuda_vigente: float
    cant_entidades: int
    cuota_total: float
    var_deuda_3m: float
    pct_normal: float
    pct_cpp: float
    pct_deficiente: float
    pct_dudoso: float
    pct_perdida: float
    alerta: bool

class ConyugeInfo(BaseModel):
    dni_hash: Optional[str] = None
    calificacion_dominante: str
    calificacion_label: str
    calificacion_color: str
    deuda_total: float
    deuda_vigente: float
    cant_entidades: int
    cuota_total: float
    pct_normal: float
    pct_cpp: float
    pct_deficiente: float
    pct_dudoso: float
    pct_perdida: float
    alerta: bool

class ResumenVinculados(BaseModel):
    total_rrll: int
    rrll_con_deuda: int
    rrll_con_mala_calif: int
    rrll_con_calificacion_riesgosa: int
    conyugue_con_mala_calif: int
    deuda_total_rrll: float
    deuda_total_conyugue: float
    deuda_total_vinculados: float
    cuota_total_empresa: float
    cuota_total_rrll: float
    cuota_total_conyugue: float
    peor_calificacion_score: int
    peor_calificacion_label: str
    semaforo: str

class VinculadosResponse(BaseModel):
    encontrado: bool
    ruc: str
    periodo: Optional[str] = None
    lista_rrll: list[RRLLInfo] = []   # uno por cada representante legal
    conyugue: Optional[ConyugeInfo] = None
    bancos: list[BancoInfo] = []
    resumen: Optional[ResumenVinculados] = None
    mensaje_copilot: str


@app.get("/api/v1/cem/vinculados/{ruc}", response_model=VinculadosResponse)
def get_vinculados(ruc: str):
    """
    Devuelve el perfil de vinculados del RUC:
    - lista_rrll: un objeto por cada representante legal (DNI distinto)
    - conyugue: calificación, deuda, cuota del cónyuge del RRLL principal
    - bancos: 3 bancos principales con saldo, atraso, tendencia
    - resumen: consolidado con semáforo de riesgo global
    """
    ruc = ruc.strip()

    def safe_str(val, default="N/D"):
        s = str(val or "").strip()
        return s if s and s.lower() not in ("nan", "none", "") else default

    def safe_float(val):
        try: return float(val or 0)
        except: return 0.0

    def safe_int(val):
        try: return int(float(val or 0))
        except: return 0

    # ── Bancos desde BD principal (base_cem_v5, RUC real) ──
    df_cem = cargar_base_datos()
    bancos: list[BancoInfo] = []
    if not df_cem.empty:
        row_cem = df_cem[df_cem["numeroruc"] == ruc]
        if not row_cem.empty:
            r = row_cem.iloc[0]
            saldo_total = sum([
                float(r.get(f"saldo_ajustado_ent_{i}", 0) or 0)
                for i in range(1, 4)
            ])
            atraso_gral = int(float(r.get("max_atraso_coloc_directas_general", -1) or -1))
            tend_raw = float(r.get("tendencia_saldo_bco_no_ibk_coloc_directas_amt_general", 1) or 1)
            tendencia = "creciente" if tend_raw > 1.05 else ("decreciente" if tend_raw < 0.95 else "estable")
            for i in range(1, 4):
                nombre = str(r.get(f"nomempresafinanc_desc_{i}", "") or "").strip()
                saldo  = float(r.get(f"saldo_ajustado_ent_{i}", 0) or 0)
                if nombre and saldo > 0 and nombre.lower() != "nan":
                    part = round(saldo / saldo_total * 100, 1) if saldo_total > 0 else None
                    bancos.append(BancoInfo(
                        nombre=nombre, saldo=round(saldo, 2),
                        participacion_pct=part,
                        atraso_dias=atraso_gral if i == 1 else None,
                        tendencia=tendencia if i == 1 else None,
                    ))

    # ── Vinculados: una fila por RRLL (numeroruc + DNI_RRLL) ──
    df_v = cargar_vinculados()
    if df_v.empty or ruc not in df_v["numeroruc"].values:
        return VinculadosResponse(
            encontrado=False, ruc=ruc, bancos=bancos,
            mensaje_copilot=(
                f"No se encontraron vinculados para RUC {ruc}. "
                + (f"Se encontraron {len(bancos)} banco(s) principal(es)." if bancos else "")
            ),
        )

    # Todas las filas del RUC en el periodo más reciente disponible
    filas_ruc = df_v[df_v["numeroruc"] == ruc].copy()
    periodo_max = filas_ruc["periodo_campania"].max()
    filas_periodo = filas_ruc[filas_ruc["periodo_campania"] == periodo_max].copy()

    # Deduplicar por DNI_RRLL (puede haber duplicados edge-case)
    filas_periodo = filas_periodo.drop_duplicates(subset="DNI_RRLL", keep="first")
    filas_periodo = filas_periodo.reset_index(drop=True)

    # ── Construir lista_rrll ──
    lista_rrll: list[RRLLInfo] = []
    for idx, row in filas_periodo.iterrows():
        calif = safe_str(row.get("rrll_calif_dominante"), "SIN_DEUDA")
        lista_rrll.append(RRLLInfo(
            numero         = len(lista_rrll) + 1,
            dni            = safe_str(row.get("DNI_RRLL")),
            cargo          = safe_str(row.get("des_cargo")),
            estado_civil   = safe_str(row.get("rrll_est_civil")),
            calificacion_dominante = calif,
            calificacion_label     = CALIF_LABEL.get(calif, calif),
            calificacion_color     = CALIF_COLOR.get(calif, "gris"),
            deuda_total    = safe_float(row.get("rrll_deuda_total")),
            deuda_vigente  = safe_float(row.get("rrll_deuda_vigente")),
            cant_entidades = safe_int(row.get("rrll_cant_entidades")),
            cuota_total    = safe_float(row.get("cuota_total_rrll")),
            var_deuda_3m   = safe_float(row.get("rrll_var_deuda_3m")),
            pct_normal     = safe_float(row.get("rrll_pct_normal")),
            pct_cpp        = safe_float(row.get("rrll_pct_cpp")),
            pct_deficiente = safe_float(row.get("rrll_pct_deficiente")),
            pct_dudoso     = safe_float(row.get("rrll_pct_dudoso")),
            pct_perdida    = safe_float(row.get("rrll_pct_perdida")),
            alerta         = calif in ("PER", "DUD", "DEF", "CPP"),
        ))

    # ── Cónyuge (del RRLL principal = fila 0) ──
    v = filas_periodo.iloc[0]
    calif_cony = safe_str(v.get("conyugue_calif_dominante"), "SIN_DEUDA")
    conyugue = ConyugeInfo(
        dni_hash       = safe_str(v.get("rrll_dni_conyugue"), None),
        calificacion_dominante = calif_cony,
        calificacion_label     = CALIF_LABEL.get(calif_cony, calif_cony),
        calificacion_color     = CALIF_COLOR.get(calif_cony, "gris"),
        deuda_total    = safe_float(v.get("conyugue_deuda_total")),
        deuda_vigente  = safe_float(v.get("conyugue_deuda_vigente")),
        cant_entidades = safe_int(v.get("conyugue_cant_entidades")),
        cuota_total    = safe_float(v.get("cuota_total_conyugue")),
        pct_normal     = safe_float(v.get("conyugue_pct_normal")),
        pct_cpp        = safe_float(v.get("conyugue_pct_cpp")),
        pct_deficiente = safe_float(v.get("conyugue_pct_deficiente")),
        pct_dudoso     = safe_float(v.get("conyugue_pct_dudoso")),
        pct_perdida    = safe_float(v.get("conyugue_pct_perdida")),
        alerta         = calif_cony in ("PER", "DUD", "DEF", "CPP"),
    )

    # ── Resumen (del primer registro, que tiene los agregados) ──
    peor = safe_int(v.get("peor_calificacion_score"))
    peor_map    = {1:"Normal", 2:"CPP", 3:"Deficiente", 4:"Dudoso", 5:"Pérdida"}
    semaforo_map= {1:"verde",  2:"amarillo", 3:"naranja", 4:"naranja", 5:"rojo"}
    rrll_mala   = safe_int(v.get("rrll_con_mala_calif"))
    resumen = ResumenVinculados(
        total_rrll                   = len(lista_rrll),
        rrll_con_deuda               = sum(1 for r in lista_rrll if r.deuda_total > 0),
        rrll_con_mala_calif          = sum(1 for r in lista_rrll if r.alerta),
        rrll_con_calificacion_riesgosa = safe_int(v.get("rrll_con_calificacion_riesgosa")),
        conyugue_con_mala_calif      = 1 if conyugue.alerta else 0,
        deuda_total_rrll             = sum(r.deuda_total for r in lista_rrll),
        deuda_total_conyugue         = conyugue.deuda_total,
        deuda_total_vinculados       = safe_float(v.get("deuda_total_vinculados")),
        cuota_total_empresa          = safe_float(v.get("cuota_total_empresa")),
        cuota_total_rrll             = sum(r.cuota_total for r in lista_rrll),
        cuota_total_conyugue         = conyugue.cuota_total,
        peor_calificacion_score      = peor,
        peor_calificacion_label      = peor_map.get(peor, "N/D"),
        semaforo                     = semaforo_map.get(peor, "gris"),
    )

    # ── Mensaje Copilot ──
    alertas = [f"RRLL {r.numero} (DNI {r.dni}): {r.calificacion_label}"
               for r in lista_rrll if r.alerta]
    if conyugue.alerta:
        alertas.append(f"Cónyuge: {conyugue.calificacion_label}")

    rrll_lines = "\n".join(
        f"  RRLL {r.numero} — DNI {r.dni} · {r.cargo} · {r.calificacion_label} · "
        f"Deuda S/ {r.deuda_total:,.2f} · Cuota S/ {r.cuota_total:,.2f}"
        for r in lista_rrll
    )
    msg = (
        f"VINCULADOS — RUC {ruc} (periodo {periodo_max})\n\n"
        f"Representantes legales ({len(lista_rrll)}):\n{rrll_lines}\n\n"
        f"Cónyuge: {conyugue.calificacion_label} · "
        f"Deuda S/ {conyugue.deuda_total:,.2f} · Cuota S/ {conyugue.cuota_total:,.2f}\n\n"
        f"Resumen: deuda vinculados S/ {resumen.deuda_total_vinculados:,.2f} · "
        f"peor calif: {resumen.peor_calificacion_label} · semáforo: {resumen.semaforo}\n"
    )
    if alertas:
        msg += "\n⚠ ALERTAS: " + " | ".join(alertas)
    if bancos:
        msg += f"\n\nBancos: {', '.join(b.nombre for b in bancos)}"

    return VinculadosResponse(
        encontrado=True,
        ruc=ruc,
        periodo=periodo_max,
        lista_rrll=lista_rrll,
        conyugue=conyugue,
        bancos=bancos,
        resumen=resumen,
        mensaje_copilot=msg,
    )
