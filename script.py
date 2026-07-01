# =============================================================================
# PIPELINE CLUSTERING IBK
# =============================================================================
# QGIS -> Complementos -> Consola de Python -> Abrir editor -> correr este archivo
#
# Toma ~834k puntos de empresas geolocalizadas con predicciones de ventas
# y genera capas de análisis: zonas comerciales densas (clusters), métricas
# de ventas por distrito, y rankings de priorización.
#
# -----------------------------------------------------------------------------
# ANTES DE EJECUTAR - leer esto
# -----------------------------------------------------------------------------
# 1. Crear una carpeta de trabajo (ej: C:\proyectos\ibk_clustering\)
#
# 2. Copiar a esa carpeta los siguientes archivos y carpetas con estos nombres exactos:
#
#      _02_predicciones_accionable_coords_nonull.gpkg
#           Capa de puntos con predicciones de ventas (layername: pred_202512)
#
#      04_clusteres_manuales.gpkg
#           Poligonos de zonas comerciales dibujados manualmente (layername: cluster)
#
#      Distrital INEI 2023\           <- carpeta completa con todos sus archivos
#          Distrital_INEI_2023.shp    (+ .dbf  .shx  .prj  .cpg  .qmd)
#           Shapefile distrital del Peru INEI 2023
#
#      Departamental INEI 2023\       <- carpeta completa con todos sus archivos
#          Departamental_INEI_2023.shp  (+ .dbf  .shx  .prj  .cpg  .qmd)
#           Shapefile departamental del Peru INEI 2023 (se carga pero no se usa en el analisis)
#
# 3. Abrir QGIS y guardar el proyecto en esa misma carpeta
#    (Archivo -> Guardar como -> elegir esa carpeta)
#    El nombre del archivo .qgz no importa, solo que este en esa carpeta.
#
# 4. Recien ahi ejecutar este script.
#    Los outputs se guardan en una subcarpeta outputs_ibk/ dentro
#    de la misma carpeta del proyecto.
#
# Si algun archivo falta o tiene un nombre distinto, el script lo
# indica al inicio antes de procesar nada.
# -----------------------------------------------------------------------------
#
# Archivos que genera en outputs_ibk/:
#   01_puntos_dbscan.gpkg
#   02_ptos_asignados_a_cluster.gpkg
#   05_clusteres_con_metricas.gpkg
#   06_ptos_standalone.gpkg
#   09_max_area_por_uid.gpkg
#   12_metricas_ventas_x_empresas_por_distrito.gpkg
#   13_metricas_standalone_por_distrito.gpkg
#   00_puntos_sin_cobertura_manual.gpkg
#   distritos_32718_fixed.gpkg
#   empresas_asignadas_a_zona_comercial.gpkg  (capa espacial para QGIS)
#   empresas_asignadas_a_zona_comercial.csv   (export limpio para Excel)
#   ranking_169_mas_todos_los_distritos.csv
# =============================================================================

import processing
import os
import csv
from datetime import datetime
from qgis.core import (
    QgsVectorLayer, QgsProject,
    QgsVectorFileWriter, QgsCoordinateReferenceSystem, NULL
)
from PyQt5.QtCore import QVariant


# =============================================================================
# CONFIGURACION
# =============================================================================

# Leer la carpeta donde esta guardado el proyecto QGIS activo.
# Si el proyecto no esta guardado, homePath() devuelve string vacio y el
# script se detiene con un mensaje claro antes de hacer nada.
PROJECT_DIR = QgsProject.instance().homePath()

if not PROJECT_DIR:
    raise Exception(
        "\n\nEl proyecto QGIS no esta guardado todavia.\n"
        "Guardalo en la carpeta de trabajo (Archivo -> Guardar como)\n"
        "y vuelve a ejecutar el script."
    )

# Nombres exactos de los archivos de entrada.
# Los dos shapefiles estan dentro de sus propias subcarpetas.
ARCHIVO_PREDICCIONES      = "_02_predicciones_accionable_coords_nonull.gpkg"
ARCHIVO_CLUSTERS_MANUALES = "04_clusteres_manuales.gpkg"
ARCHIVO_DISTRITOS         = os.path.join("Distrital INEI 2023",     "Distrital_INEI_2023.shp")
ARCHIVO_DEPARTAMENTAL     = os.path.join("Departamental INEI 2023", "Departamental_INEI_2023.shp")

# Verificar que todos los archivos existen antes de arrancar el pipeline
_faltantes = [
    a for a in [ARCHIVO_PREDICCIONES, ARCHIVO_CLUSTERS_MANUALES,
                ARCHIVO_DISTRITOS, ARCHIVO_DEPARTAMENTAL]
    if not os.path.exists(os.path.join(PROJECT_DIR, a))
]
if _faltantes:
    raise Exception(
        f"\n\nArchivos faltantes en {PROJECT_DIR}:\n" +
        "\n".join(f"  - {a}" for a in _faltantes) +
        "\n\nCopialos a esa carpeta y vuelve a ejecutar."
    )

# Construccion de rutas completas
PREDICCIONES_PATH      = os.path.join(PROJECT_DIR, ARCHIVO_PREDICCIONES)      + "|layername=pred_202512"
CLUSTERS_MANUALES_PATH = os.path.join(PROJECT_DIR, ARCHIVO_CLUSTERS_MANUALES) + "|layername=cluster"
DISTRITOS_PATH         = os.path.join(PROJECT_DIR, ARCHIVO_DISTRITOS)
DEPARTAMENTAL_PATH     = os.path.join(PROJECT_DIR, ARCHIVO_DEPARTAMENTAL)

# Los outputs van a una subcarpeta dentro del proyecto - se crea si no existe
OUTPUT_DIR = os.path.join(PROJECT_DIR, "outputs_ibk")

# Parametros DBSCAN
# EPS: radio en metros para buscar puntos vecinos
# MIN_SIZE: minimo de puntos para formar un cluster
#
# Con EPS=100 y MIN_SIZE=100 el resultado es:
#   131,300 puntos agrupados en clusters
#   703,094 puntos sin cluster (zonas dispersas)
#
# Subir EPS o bajar MIN_SIZE agrupa mas puntos (clusters mas grandes).
# Bajar EPS o subir MIN_SIZE es mas restrictivo (menos puntos agrupados).
#
# DBSCAN*=False incluye los puntos de borde en el cluster mas cercano.
# DBSCAN*=True los descarta como ruido - con estos datos da solo 84,258
# puntos, que es incorrecto.
DBSCAN_EPS      = 100
DBSCAN_MIN_SIZE = 100

# UTM zona 18S, adecuado para Peru continental
CRS_TRABAJO = "EPSG:32718"

# Lista de 169 ubigeos prioritarios para el ranking (paso 16).
# Estos van primero en el CSV en este orden exacto, sin importar sus ventas.
# El resto de los ~1600 distritos se agrega despues, ordenado por ventas desc.
# No modificar el orden a menos que cambie el criterio de priorizacion.
ORDEN_RANKING_169 = [
    '150101','130101','150103','150132','150140','150115','150135','150131','140101','150122',
    '150117','211101','150110','150142','040101','200101','150130','110101','070101','150125',
    '150108','150137','060101','150133','080101','120101','021801','220901','150143','040104',
    '150114','150113','230101','150136','150112','250101','170101','050101','080108','070106',
    '150141','150106','150116','200601','150120','210101','150118','160101','150119','040129',
    '150105','040112','130102','150801','120114','100101','150121','140105','020101','080105',
    '150111','200104','140301','040103','110201','230110','150104','150128','160112','240101',
    '080104','120107','021809','130111','150134','070102','110106','220910','040122','040110',
    '200701','150102','130105','040126','150107','140106','200115','020105','150109','150123',
    '220909','130104','040123','040107','040109','040102','080106','070103','150806','060108',
    '140108','070104','160108','120125','200105','130103','230102','150810','040117','150124',
    '250107','200702','140112','050115','100102','160113','110102','130106','250105','230104',
    '150126','130107','230108','110103','150129','110107','170102','150139','200602','040124',
    '110111','120119','021808','200703','040105','110108','110206','240102','170103','200706',
    '040128','050104','110112','110110','150138','120134','170104','220903','120121','220908',
    '150127','070107','110210','150805','050116','120133','100111','070105','200705','150803',
    '140118','060106','040116','060107','240106','200704','230106','060109','220912',
]


# =============================================================================
# FUNCIONES AUXILIARES
# =============================================================================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def cargar(path, nombre):
    """Carga una capa vectorial desde disco y la agrega al proyecto activo."""
    capa = QgsVectorLayer(path, nombre, "ogr")
    if not capa.isValid():
        raise Exception(f"No se pudo cargar: {path}")
    QgsProject.instance().addMapLayer(capa)
    log(f"  {nombre} ({capa.featureCount()} features)")
    return capa


def guardar(layer, nombre, archivo):
    """
    Escribe una capa temporal a disco y la vuelve a cargar.
    Se usa cuando la misma capa se necesita en varios pasos del pipeline,
    ya que QGIS puede fallar al reutilizar capas temporales mas de una vez.
    """
    path = os.path.join(OUTPUT_DIR, archivo)
    QgsVectorFileWriter.writeAsVectorFormat(layer, path, "UTF-8", layer.crs(), "GPKG")
    return cargar(path, nombre)


def qgis_a_python(valor):
    """
    Convierte el NULL de QGIS a None de Python.
    QGIS tiene su propio tipo NULL distinto de None, lo que puede causar
    errores al escribir CSVs o al comparar valores numericos.
    """
    return None if valor == NULL or valor is None else valor


def exportar_csv(layer, campos, nombre_archivo):
    """
    Exporta campos seleccionados de una capa a CSV en OUTPUT_DIR.
    Omite los campos que no existan en la capa.
    Encoding utf-8-sig para compatibilidad con Excel.
    """
    disponibles = {f.name() for f in layer.fields()}
    campos_ok   = [campo for campo in campos if campo in disponibles]
    faltantes   = [campo for campo in campos if campo not in disponibles]
    if faltantes:
        log(f"  AVISO - campos no encontrados en {layer.name()}: {faltantes}")
    ruta = os.path.join(OUTPUT_DIR, nombre_archivo)
    with open(ruta, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=campos_ok)
        writer.writeheader()
        for feat in layer.getFeatures():
            writer.writerow({campo: qgis_a_python(feat[campo]) for campo in campos_ok})
    log(f"  guardado: {nombre_archivo} ({layer.featureCount()} filas)")
    return ruta


def mover_a_grupo(layer, grupo):
    """
    Mueve una capa del nivel raiz del panel de capas al grupo indicado.
    Clona el nodo, lo inserta en el grupo y elimina el original del raiz.
    """
    root = QgsProject.instance().layerTreeRoot()
    nodo = root.findLayer(layer.id())
    if nodo:
        clon = nodo.clone()
        grupo.insertChildNode(-1, clon)
        nodo.parent().removeChildNode(nodo)


# =============================================================================
# PIPELINE
# =============================================================================

def ejecutar_pipeline():

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log("=" * 60)
    log("INICIANDO PIPELINE CLUSTERING IBK")
    log("=" * 60)

    # ------------------------------------------------------------------
    # PASO 1 - Cargar capas base
    # Las predicciones ya estan en EPSG:32718.
    # Los distritos vienen en EPSG:4326 y se reproyectan en el paso 2.
    # ------------------------------------------------------------------
    log("PASO 1: Cargando capas base...")

    predicciones      = cargar(PREDICCIONES_PATH,      "predicciones_raw")
    clusters_manuales = cargar(CLUSTERS_MANUALES_PATH, "04_clusteres_manuales")
    distritos         = cargar(DISTRITOS_PATH,         "Distrital_INEI_2023")
    departamental     = cargar(DEPARTAMENTAL_PATH,     "Departamental_INEI_2023")

    # ------------------------------------------------------------------
    # PASO 2 - Reproyectar distritos a UTM 18S
    # Todas las capas deben estar en el mismo CRS para los analisis
    # espaciales. Solo los distritos necesitan conversion.
    # ------------------------------------------------------------------
    log("PASO 2: Reproyectando distritos a EPSG:32718...")

    crs_utm = QgsCoordinateReferenceSystem(CRS_TRABAJO)

    distritos_utm = processing.run("native:reprojectlayer", {
        'INPUT':      distritos,
        'TARGET_CRS': crs_utm,
        'OUTPUT':     'TEMPORARY_OUTPUT'
    })['OUTPUT']

    log("  distritos reproyectados")

    # ------------------------------------------------------------------
    # PASO 3 - Clustering DBSCAN
    # Agrupa los ~834k puntos por densidad geografica. Cada punto queda
    # con un CLUSTER_ID (numero entero) si fue agrupado, o NULL si no.
    # Tambien agrega CLUSTER_SIZE con el total de puntos del cluster.
    # Ver parametros DBSCAN_EPS y DBSCAN_MIN_SIZE en CONFIG.
    # ------------------------------------------------------------------
    log(f"PASO 3: DBSCAN (eps={DBSCAN_EPS}m, min_size={DBSCAN_MIN_SIZE})...")
    log("  puede demorar varios minutos...")

    dbscan_result = processing.run("native:dbscanclustering", {
        'INPUT':           predicciones,
        'EPS':             DBSCAN_EPS,
        'MIN_SIZE':        DBSCAN_MIN_SIZE,
        'DBSCAN*':         False,          # False = incluir puntos de borde en clusters
        'FIELD_NAME':      'CLUSTER_ID',
        'SIZE_FIELD_NAME': 'CLUSTER_SIZE',
        'OUTPUT':          'TEMPORARY_OUTPUT'
    })

    # Se guarda a disco porque se reutiliza en pasos 4 y 13
    ptos_dbscan = guardar(
        dbscan_result['OUTPUT'], "01_puntos_dbscan", "01_puntos_dbscan.gpkg"
    )
    log(f"  clusters detectados: {dbscan_result['NUM_CLUSTERS']}")

    # ------------------------------------------------------------------
    # PASO 4 - Separar puntos agrupados
    # De los ~834k puntos, ~131k quedaron con CLUSTER_ID asignado.
    # Estos son el insumo para calcular metricas de zonas comerciales.
    # Se guarda a disco porque se reutiliza en pasos 5, 12 y 18.
    # ------------------------------------------------------------------
    log("PASO 4: Filtrando puntos con CLUSTER_ID asignado...")

    ptos_asignados_raw = processing.run("native:extractbyexpression", {
        'INPUT':      ptos_dbscan,
        'EXPRESSION': '"CLUSTER_ID" IS NOT NULL',
        'OUTPUT':     'TEMPORARY_OUTPUT'
    })['OUTPUT']

    ptos_asignados = guardar(
        ptos_asignados_raw,
        "02_ptos_asignados_a_cluster",
        "02_ptos_asignados_a_cluster.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 5 - Metricas de ventas por zona comercial
    # Para cada poligono de zona comercial calcula, sobre los puntos
    # que caen dentro: cantidad de empresas (_count), ventas totales
    # (_sum) y venta promedio por empresa (_mean). Tanto para ventas
    # mensuales como anuales.
    # Se guarda a disco porque se reutiliza en pasos 7 y 18.
    # ------------------------------------------------------------------
    log("PASO 5: Calculando metricas de ventas por zona comercial...")

    metricas_raw = processing.run("qgis:joinbylocationsummary", {
        'INPUT':               clusters_manuales,
        'JOIN':                ptos_asignados,
        'PREDICATE':           [0],          # intersects
        'JOIN_FIELDS':         ['prediccion_con_factor_mensual', 'prediccion_con_factor_anual'],
        'SUMMARIES':           [0, 5, 6],    # count, sum, mean
        'DISCARD_NONMATCHING': False,
        'OUTPUT':              'TEMPORARY_OUTPUT'
    })['OUTPUT']

    clusteres_con_metricas = guardar(
        metricas_raw,
        "05_clusteres_con_metricas_temp",
        "_temp_clusteres_con_metricas.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 6 - Corregir geometrias de distritos
    # El shapefile de INEI tiene geometrias con errores (auto-intersecciones,
    # anillos invalidos, etc.) que hacen fallar la interseccion del paso 7.
    # fixgeometries los repara sin tocar los datos.
    # Se guarda a disco porque se reutiliza en pasos 7, 11 y 14.
    # ------------------------------------------------------------------
    log("PASO 6: Corrigiendo geometrias de distritos...")

    distritos_fixed_raw = processing.run("native:fixgeometries", {
        'INPUT':  distritos_utm,
        'OUTPUT': 'TEMPORARY_OUTPUT'
    })['OUTPUT']

    distritos_fixed = guardar(
        distritos_fixed_raw, "distritos_32718_fixed", "distritos_32718_fixed.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 7 - Intersectar zonas comerciales con distritos
    # Una zona puede cruzar el limite entre dos distritos. Este paso
    # la corta por esos limites, generando un fragmento por cada distrito
    # que toca. Despues (pasos 8 y 9) se identifica cual fragmento es
    # el mas grande para asignarle un distrito a la zona.
    # ------------------------------------------------------------------
    log("PASO 7: Intersectando zonas comerciales con distritos...")

    interseccion = processing.run("native:intersection", {
        'INPUT':   clusteres_con_metricas,
        'OVERLAY': distritos_fixed,
        'OUTPUT':  'TEMPORARY_OUTPUT'
    })['OUTPUT']

    # Calcular el area de cada fragmento para identificar el mas grande
    interseccion_area = processing.run("native:fieldcalculator", {
        'INPUT':      interseccion,
        'FIELD_NAME': 'area_interseccion',
        'FIELD_TYPE': 0,        # decimal
        'FORMULA':    '$area',  # area en m2
        'OUTPUT':     'TEMPORARY_OUTPUT'
    })['OUTPUT']

    log("  interseccion completada")

    # ------------------------------------------------------------------
    # PASO 8 - Area maxima por zona (uid_poly)
    # Por cada uid_poly calcula el maximo de area_interseccion entre
    # todos sus fragmentos. Ese maximo identifica al distrito dominante.
    # ------------------------------------------------------------------
    log("PASO 8: Calculando area maxima por zona comercial...")

    stats_raw = processing.run("qgis:statisticsbycategories", {
        'INPUT':                 interseccion_area,
        'VALUES_FIELD_NAME':     'area_interseccion',
        'CATEGORIES_FIELD_NAME': ['uid_poly'],
        'OUTPUT':                'TEMPORARY_OUTPUT'
    })['OUTPUT']

    stats = guardar(stats_raw, "09_max_area_por_uid", "09_max_area_por_uid.gpkg")

    # ------------------------------------------------------------------
    # PASO 9 - Distrito dominante por zona comercial
    # Join con la tabla de maximos y filtro del fragmento cuya area
    # coincide con el maximo. La tolerancia de 0.01 m2 evita que
    # errores de punto flotante descarten el fragmento correcto.
    # ------------------------------------------------------------------
    log("PASO 9: Asignando distrito dominante por zona comercial...")

    con_max = processing.run("native:joinattributestable", {
        'INPUT':          interseccion_area,
        'FIELD':          'uid_poly',
        'INPUT_2':        stats,
        'FIELD_2':        'uid_poly',
        'FIELDS_TO_COPY': ['max'],
        'METHOD':         1,
        'OUTPUT':         'TEMPORARY_OUTPUT'
    })['OUTPUT']

    distrito_dominante = processing.run("native:extractbyexpression", {
        'INPUT':      con_max,
        'EXPRESSION': 'abs("area_interseccion" - "max") < 0.01',
        'OUTPUT':     'TEMPORARY_OUTPUT'
    })['OUTPUT']

    log(f"  {distrito_dominante.featureCount()} zonas con distrito asignado")

    # ------------------------------------------------------------------
    # PASO 10 - Capa final de zonas comerciales
    # Une las metricas de ventas (clusteres_con_metricas) con la
    # ubicacion geografica (distrito_dominante) usando uid_poly.
    # ------------------------------------------------------------------
    log("PASO 10: Generando capa final de zonas comerciales...")

    clusteres_final_raw = processing.run("native:joinattributestable", {
        'INPUT':          clusteres_con_metricas,
        'FIELD':          'uid_poly',
        'INPUT_2':        distrito_dominante,
        'FIELD_2':        'uid_poly',
        'FIELDS_TO_COPY': ['UBIGEO', 'DEPARTAMEN', 'PROVINCIA', 'DISTRITO'],
        'METHOD':         1,
        'OUTPUT':         'TEMPORARY_OUTPUT'
    })['OUTPUT']

    clusteres_final = guardar(
        clusteres_final_raw,
        "05_clusteres_con_metricas",
        "05_clusteres_con_metricas.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 10b - Sectores CIIU por zona comercial
    #
    # Agrega 4 campos a 05_clusteres_con_metricas:
    #
    #   sector_predominante
    #       Sector economico con mas empresas dentro de la zona comercial.
    #       Ej: "comercio" significa que la mayoria de empresas en esa zona
    #       son del rubro comercial.
    #
    #   concentracion_sector_pct
    #       Porcentaje de empresas que pertenecen al sector predominante.
    #       Ej: 80.5 significa que 8 de cada 10 empresas son del mismo sector.
    #       Valores altos (>70%) indican zonas especializadas.
    #       Valores bajos (<40%) indican zonas con actividad economica diversa.
    #
    # Por que no se usa statisticsbycategories:
    #   Ese algoritmo no calcula 'majority' para campos de texto, solo produce
    #   min/max alfabetico. Se usa Counter de Python para calcular correctamente.
    # ------------------------------------------------------------------
    log("PASO 10b: Calculando sectores CIIU por zona comercial...")

    from collections import Counter
    from qgis.core import QgsField
    from PyQt5.QtCore import QVariant

    # Asociar cada punto clusterizado al uid_poly del poligono que lo contiene
    ptos_con_uid = processing.run("native:joinattributesbylocation", {
        'INPUT':       ptos_asignados,
        'JOIN':        clusters_manuales,
        'PREDICATE':   [0],          # intersects
        'JOIN_FIELDS': ['uid_poly'],
        'METHOD':      0,
        'OUTPUT':      'TEMPORARY_OUTPUT'
    })['OUTPUT']

    # Contar empresas por sector para cada uid_poly
    conteos = {}
    for feat in ptos_con_uid.getFeatures():
        uid  = feat['uid_poly']
        ciiu = feat['sector_ciiu']
        if uid is None:
            continue
        if uid not in conteos:
            conteos[uid] = Counter()
        if ciiu and str(ciiu).strip():
            conteos[uid][str(ciiu)] += 1

    def sector_predominante_y_concentracion(counter):
        """
        Dado un Counter de sectores, devuelve (sector_predominante, pct).
        Zonas sin empresas devuelven (None, None).
        """
        if not counter:
            return None, None
        top   = counter.most_common(1)
        total = sum(counter.values())
        sector = top[0][0]
        pct    = round(top[0][1] / total * 100, 1) if total > 0 else None
        return sector, pct

    ciiu_por_cluster = {
        uid: sector_predominante_y_concentracion(cnt)
        for uid, cnt in conteos.items()
    }

    # Agregar los 2 campos nuevos a la capa y escribir los valores
    clusteres_final.startEditing()

    campos_ciiu = [
        ('sector_predominante',      QVariant.String),
        ('concentracion_sector_pct', QVariant.Double),
    ]
    for nombre, tipo in campos_ciiu:
        if clusteres_final.fields().indexFromName(nombre) < 0:
            clusteres_final.addAttribute(QgsField(nombre, tipo))
    clusteres_final.updateFields()

    idx_campos = {nombre: clusteres_final.fields().indexFromName(nombre)
                  for nombre, _ in campos_ciiu}

    for feat in clusteres_final.getFeatures():
        uid = feat['uid_poly']
        sector, pct = ciiu_por_cluster.get(uid, (None, None))
        clusteres_final.changeAttributeValue(feat.id(), idx_campos['sector_predominante'],      sector)
        clusteres_final.changeAttributeValue(feat.id(), idx_campos['concentracion_sector_pct'], pct)

    clusteres_final.commitChanges()

    n_con_ciiu = sum(1 for v in ciiu_por_cluster.values() if v[0] is not None)
    log(f"  {n_con_ciiu} zonas comerciales con sector CIIU calculado")

    # ------------------------------------------------------------------
    # PASO 11 - Metricas de ventas por distrito (todas las empresas)
    # A diferencia del paso 5, aqui se usan los ~834k puntos completos,
    # no solo los 131k clusterizados. Con los 131k solo 108 de los 1891
    # distritos tendrian datos; con todos se cubren 1831 distritos.
    # ------------------------------------------------------------------
    log("PASO 11: Calculando metricas de ventas por distrito...")

    metricas_dist_raw = processing.run("qgis:joinbylocationsummary", {
        'INPUT':               distritos_fixed,
        'JOIN':                predicciones,
        'PREDICATE':           [0],
        'JOIN_FIELDS':         ['prediccion_con_factor_mensual', 'prediccion_con_factor_anual'],
        'SUMMARIES':           [0, 5, 6],
        'DISCARD_NONMATCHING': False,
        'OUTPUT':              'TEMPORARY_OUTPUT'
    })['OUTPUT']

    metricas_dist = guardar(
        metricas_dist_raw,
        "12_metricas_ventas_x_empresas_por_distrito",
        "12_metricas_ventas_x_empresas_por_distrito.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 12 - Validacion: puntos clusterizados sin cobertura manual
    # Comprueba que los ~131k puntos agrupados por DBSCAN esten dentro
    # de algun poligono manual. Si hay puntos fuera, hay actividad densa
    # en una zona donde todavia no se dibujo un poligono.
    # Con la data actual deberia salir vacio o casi vacio.
    # ------------------------------------------------------------------
    log("PASO 12: Validando puntos clusterizados fuera de poligonos manuales...")

    puntos_fuera_raw = processing.run("native:extractbylocation", {
        'INPUT':     ptos_asignados,
        'PREDICATE': [2],          # disjoint - no toca ningun poligono
        'INTERSECT': clusters_manuales,
        'OUTPUT':    'TEMPORARY_OUTPUT'
    })['OUTPUT']

    puntos_fuera = guardar(
        puntos_fuera_raw,
        "00_puntos_sin_cobertura_manual",
        "00_puntos_sin_cobertura_manual.gpkg"
    )

    n_fuera = puntos_fuera.featureCount()

    # ------------------------------------------------------------------
    # PASO 13 - Empresas standalone (sin cluster DBSCAN)
    # Los ~703k puntos con CLUSTER_ID = NULL son empresas en zonas
    # dispersas que DBSCAN no agrupó. Se usan en el paso 14.
    # ------------------------------------------------------------------
    log("PASO 13: Extrayendo empresas standalone (CLUSTER_ID = NULL)...")

    ptos_standalone_raw = processing.run("native:extractbyexpression", {
        'INPUT':      ptos_dbscan,
        'EXPRESSION': '"CLUSTER_ID" IS NULL',
        'OUTPUT':     'TEMPORARY_OUTPUT'
    })['OUTPUT']

    ptos_standalone = guardar(
        ptos_standalone_raw,
        "06_ptos_standalone",
        "06_ptos_standalone.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 14 - Metricas distritales de empresas standalone
    # Mismo calculo que el paso 11 pero solo con empresas standalone.
    # Sirve para saber cuanta actividad comercial de cada distrito viene
    # de zonas densas (clusters) vs. empresas dispersas.
    # ------------------------------------------------------------------
    log("PASO 14: Calculando metricas standalone por distrito...")

    metricas_standalone_raw = processing.run("qgis:joinbylocationsummary", {
        'INPUT':               distritos_fixed,
        'JOIN':                ptos_standalone,
        'PREDICATE':           [0],
        'JOIN_FIELDS':         ['prediccion_con_factor_mensual', 'prediccion_con_factor_anual'],
        'SUMMARIES':           [0, 5, 6],
        'DISCARD_NONMATCHING': False,
        'OUTPUT':              'TEMPORARY_OUTPUT'
    })['OUTPUT']

    metricas_standalone = guardar(
        metricas_standalone_raw,
        "13_metricas_standalone_por_distrito",
        "13_metricas_standalone_por_distrito.gpkg"
    )

    # ------------------------------------------------------------------
    # PASO 15 - Aliases de campos
    # Renombra los campos en la tabla de atributos de QGIS para que sean
    # legibles. Solo afecta la visualizacion, no el archivo en disco.
    # ------------------------------------------------------------------
    log("PASO 15: Aplicando aliases a campos...")

    ALIASES_COMUNES = {
        # campos geograficos
        'UBIGEO':     'Ubigeo',
        'DEPARTAMEN': 'Departamento',
        'PROVINCIA':  'Provincia',
        'DISTRITO':   'Distrito',
        'ubigeo':     'Ubigeo',
        'departamento': 'Departamento',
        'provincia':  'Provincia',
        'distrito':   'Distrito',
        'zona_comercial': 'Zona comercial',
        # metricas de ventas y empresas
        'prediccion_con_factor_mensual_count': 'Número de empresas',
        'prediccion_con_factor_mensual_sum':   'Ventas mensuales totales',
        'prediccion_con_factor_mensual_mean':  'Ventas mensuales promedio',
        'prediccion_con_factor_anual_count':   'Número de empresas',
        'prediccion_con_factor_anual_sum':     'Ventas anuales totales',
        'prediccion_con_factor_anual_mean':    'Ventas anuales promedio',
        # campos en capa de empresas (nombres del refactorfields)
        'cluster_empresas_count':    'Número de empresas en la zona',
        'cluster_ventas_mens_total': 'Ventas mensuales totales de la zona',
        'cluster_ventas_mens_prom':  'Ventas mensuales promedio de la zona',
        'cluster_ventas_anu_total':  'Ventas anuales totales de la zona',
        'cluster_ventas_anu_prom':   'Ventas anuales promedio de la zona',
        # sector economico
        'sector_predominante':       'Sector económico principal',
        'concentracion_sector_pct':  '% Empresas del sector principal',
    }

    def aplicar_aliases(capa, aliases):
        for nombre_campo, alias in aliases.items():
            idx = capa.fields().indexFromName(nombre_campo)
            if idx >= 0:
                capa.setFieldAlias(idx, alias)
        log(f"  aliases en: {capa.name()}")

    aplicar_aliases(clusteres_final,     ALIASES_COMUNES)
    aplicar_aliases(metricas_dist,       ALIASES_COMUNES)
    aplicar_aliases(metricas_standalone, ALIASES_COMUNES)

    # ------------------------------------------------------------------
    # PASO 15b - Campos formateados para visualizacion (popups webmap)
    #
    # Los campos numericos originales se quedan intactos para analisis,
    # pero se agregan campos de texto formateados que son los que se
    # muestran en los popups del webmap cuando se exporta con qgis2web.
    #
    # Campos nuevos agregados a cada capa de metricas:
    #
    #   empresas_display
    #       Numero de empresas con separador de miles.
    #       Ej: 980 -> "980", 12450 -> "12,450"
    #
    #   ventas_mens_total_display / ventas_mens_prom_display
    #   ventas_anu_total_display  / ventas_anu_prom_display
    #       Ventas en miles de soles con separador de miles y sufijo "mil".
    #       Ej: 46,867,268 -> "S/ 46,867 mil"
    #            47,823    -> "S/ 48 mil"   (redondeado a 0 decimales)
    #
    # Por que campos separados y no solo formato de aliases:
    #   qgis2web exporta los valores numericos raw al popup, no respeta
    #   configuraciones de formato de QGIS. La unica forma de que el popup
    #   muestre "S/ 46,867 mil" es tener el valor ya como string en la capa.
    #
    # En la tabla de atributos de QGIS se ven ambos campos: el numero
    # completo original (para filtrar/ordenar) y el formateado (para leer).
    # En el CSV exportado solo van los numeros originales (sin formato)
    # para que quien abra el archivo pueda hacer analisis numerico libre.
    # ------------------------------------------------------------------
    log("PASO 15b: Agregando campos formateados para visualizacion...")

    def fmt_empresas(n):
        """Numero entero con separador de miles. 12450 -> '12,450'"""
        if n is None or str(n) == 'NULL':
            return None
        try:
            return f"{int(n):,}"
        except (ValueError, TypeError):
            return None

    def fmt_ventas_miles(v):
        """Monto en miles de soles con separador. 46867268 -> 'S/ 46,867 mil'"""
        if v is None or str(v) == 'NULL':
            return None
        try:
            return f"S/ {float(v) / 1000:,.0f} mil"
        except (ValueError, TypeError):
            return None

    def agregar_campos_formato(capa, campo_count, campos_ventas):
        """
        Agrega a una capa los campos formateados de visualizacion.
        campo_count: nombre del campo origen con el numero de empresas (int)
        campos_ventas: dict {campo_origen: campo_destino_display}
        """
        capa.startEditing()

        # Los campos formateados son todos tipo string (llevan sufijos/separadores)
        campos_nuevos = [('empresas_display', QVariant.String)]
        for _, destino in campos_ventas.items():
            campos_nuevos.append((destino, QVariant.String))
        for nombre, tipo in campos_nuevos:
            if capa.fields().indexFromName(nombre) < 0:
                capa.addAttribute(QgsField(nombre, tipo))
        capa.updateFields()

        campos_existentes = {f.name() for f in capa.fields()}
        idx_empresas = capa.fields().indexFromName('empresas_display')
        idx_ventas   = {destino: capa.fields().indexFromName(destino)
                        for destino in campos_ventas.values()}

        for feat in capa.getFeatures():
            if campo_count in campos_existentes:
                capa.changeAttributeValue(feat.id(), idx_empresas, fmt_empresas(feat[campo_count]))
            for origen, destino in campos_ventas.items():
                if origen in campos_existentes:
                    capa.changeAttributeValue(feat.id(), idx_ventas[destino], fmt_ventas_miles(feat[origen]))

        capa.commitChanges()
        log(f"  campos formateados agregados a: {capa.name()}")

    # Mapeo campo origen -> campo destino display para las capas de metricas agregadas
    CAMPOS_VENTAS_AGREGADAS = {
        'prediccion_con_factor_mensual_sum':  'ventas_mens_total_display',
        'prediccion_con_factor_mensual_mean': 'ventas_mens_prom_display',
        'prediccion_con_factor_anual_sum':    'ventas_anu_total_display',
        'prediccion_con_factor_anual_mean':   'ventas_anu_prom_display',
    }

    agregar_campos_formato(clusteres_final,     'prediccion_con_factor_mensual_count', CAMPOS_VENTAS_AGREGADAS)
    agregar_campos_formato(metricas_dist,       'prediccion_con_factor_mensual_count', CAMPOS_VENTAS_AGREGADAS)
    agregar_campos_formato(metricas_standalone, 'prediccion_con_factor_mensual_count', CAMPOS_VENTAS_AGREGADAS)

    # Aliases legibles para los campos formateados
    ALIASES_FORMATO = {
        'empresas_display':           'Número de empresas',
        'ventas_mens_total_display':  'Ventas mensuales totales (miles S/)',
        'ventas_mens_prom_display':   'Ventas mensuales promedio (miles S/)',
        'ventas_anu_total_display':   'Ventas anuales totales (miles S/)',
        'ventas_anu_prom_display':    'Ventas anuales promedio (miles S/)',
    }
    aplicar_aliases(clusteres_final,     ALIASES_FORMATO)
    aplicar_aliases(metricas_dist,       ALIASES_FORMATO)
    aplicar_aliases(metricas_standalone, ALIASES_FORMATO)

    # ------------------------------------------------------------------
    # PASO 16 - Exportar capas de resultado a CSV
    #
    # Genera CSVs para las dos capas que no tenian export todavia.
    # Los otros dos (ranking y empresas) se generan en pasos siguientes.
    # ------------------------------------------------------------------
    log("PASO 16: Exportando capas de resultado a CSV...")

    CAMPOS_CLUSTERS_CSV = [
        'zona_comercial', 'uid_poly', 'UBIGEO', 'DEPARTAMEN', 'PROVINCIA', 'DISTRITO',
        'prediccion_con_factor_mensual_count', 'prediccion_con_factor_mensual_sum',
        'prediccion_con_factor_mensual_mean',
        'prediccion_con_factor_anual_count',   'prediccion_con_factor_anual_sum',
        'prediccion_con_factor_anual_mean',
        # sector economico de la zona (calculados en PASO 10b)
        'sector_predominante',       # sector con mas empresas en la zona
        'concentracion_sector_pct',  # % de empresas del sector predominante
    ]
    exportar_csv(clusteres_final, CAMPOS_CLUSTERS_CSV, "zonas_comerciales_con_metricas.csv")

    CAMPOS_STANDALONE_CSV = [
        'UBIGEO', 'DEPARTAMEN', 'PROVINCIA', 'DISTRITO',
        'prediccion_con_factor_mensual_count', 'prediccion_con_factor_mensual_sum',
        'prediccion_con_factor_mensual_mean',
        'prediccion_con_factor_anual_count',   'prediccion_con_factor_anual_sum',
        'prediccion_con_factor_anual_mean',
    ]
    exportar_csv(metricas_standalone, CAMPOS_STANDALONE_CSV, "standalone_metricas_por_distrito.csv")

    # ------------------------------------------------------------------
    # PASO 17 - CSV ranking de distritos
    # Genera un CSV con todos los distritos (~1800) en dos bloques:
    #   - Los 169 de ORDEN_RANKING_169 primero, en ese orden fijo
    #   - El resto ordenado por ventas mensuales totales desc
    # Los distritos sin datos de ventas quedan al final.
    # Fuente: capa 12, que usa todas las predicciones para maxima cobertura.
    # Encoding utf-8-sig para que Excel abra las tildes correctamente.
    # ------------------------------------------------------------------
    log("PASO 17: Generando CSV ranking de distritos...")

    filas_dist = {}
    for feat in metricas_dist.getFeatures():
        ubigeo = str(qgis_a_python(feat['UBIGEO']) or '').strip()
        filas_dist[ubigeo] = {
            'ubigeo':                 ubigeo,
            'departamento':           qgis_a_python(feat['DEPARTAMEN']) or '',
            'provincia':              qgis_a_python(feat['PROVINCIA'])  or '',
            'distrito':               qgis_a_python(feat['DISTRITO'])   or '',
            'ventas_totales_mensual': qgis_a_python(feat['prediccion_con_factor_mensual_sum']),
            'numero_de_empresas':     qgis_a_python(feat['prediccion_con_factor_mensual_count']),
        }

    set_ranking = set(ORDEN_RANKING_169)
    top_169     = [filas_dist[u] for u in ORDEN_RANKING_169 if u in filas_dist]
    resto       = [v for k, v in filas_dist.items() if k not in set_ranking]

    # Avisar si algun ubigeo del ranking no aparece en los datos
    faltantes = [u for u in ORDEN_RANKING_169 if u not in filas_dist]
    if faltantes:
        log(f"  AVISO - ubigeos del ranking sin datos: {faltantes}")

    # None al final, el resto desc por ventas
    resto.sort(key=lambda x: (
        x['ventas_totales_mensual'] is None,
        -(x['ventas_totales_mensual'] or 0)
    ))

    ranking_csv_path = os.path.join(OUTPUT_DIR, "ranking_169_mas_todos_los_distritos.csv")
    campos_ranking   = ['ubigeo', 'departamento', 'provincia', 'distrito',
                        'ventas_totales_mensual', 'numero_de_empresas']

    with open(ranking_csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=campos_ranking)
        writer.writeheader()
        writer.writerows(top_169 + resto)

    log(f"  {len(top_169)} prioritarios + {len(resto)} adicionales")
    log(f"  guardado en: {ranking_csv_path}")

    # ------------------------------------------------------------------
    # PASO 18 - CSV y capa GPKG: empresas con zona comercial asignada
    # Para cada una de las ~131k empresas clusterizadas, determina en que
    # zona comercial (poligono manual) esta ubicada y le agrega las
    # metricas de ventas de esa zona.
    #
    # Por que join espacial y no por CLUSTER_ID:
    # El CLUSTER_ID de DBSCAN no tiene ninguna relacion con el uid_poly
    # de los poligonos manuales - son sistemas independientes. La unica
    # forma de saber "en que zona esta esta empresa" es por geometria.
    #
    # Se hace en dos pasos:
    #   17a. Join espacial: punto -> poligono que lo contiene
    #        agrega zona_comercial y uid_poly
    #   17b. Join tabular por uid_poly -> metricas del cluster
    #        agrega _count/_sum/_mean de ventas
    #   17c. Exportar CSV con campos seleccionados + guardar GPKG para QGIS
    # ------------------------------------------------------------------
    log("PASO 18: Generando capa de empresas con zona comercial asignada...")

    # 17a - join espacial
    # PREDICATE=[0] = intersects. Se usa intersects y no within porque algunos puntos
    # caen exactamente sobre el borde del poligono y within los descarta, dejandolos
    # con zona_comercial NULL. Intersects los captura correctamente.
    # METHOD=1 = si un punto toca dos poligonos a la vez (borde compartido), tomar el primero
    # DISCARD_NONMATCHING=False = conservar puntos sin poligono (no deberia haber ninguno
    #   si 00_puntos_sin_cobertura_manual esta vacio, pero por si acaso)
    log("  17a: join espacial puntos -> zonas comerciales...")
    empresas_con_zona_raw = processing.run("native:joinattributesbylocation", {
        'INPUT':               ptos_asignados,
        'PREDICATE':           [0],
        'JOIN':                clusters_manuales,
        'JOIN_FIELDS':         ['zona_comercial', 'uid_poly'],
        'METHOD':              1,
        'DISCARD_NONMATCHING': False,
        'PREFIX':              '',
        'OUTPUT':              'TEMPORARY_OUTPUT'
    })['OUTPUT']

    # 17b - join tabular con metricas
    log("  17b: join tabular uid_poly -> metricas de la zona...")
    METRICAS_CLUSTER = [
        'prediccion_con_factor_mensual_count',
        'prediccion_con_factor_mensual_sum',
        'prediccion_con_factor_mensual_mean',
        'prediccion_con_factor_anual_sum',
        'prediccion_con_factor_anual_mean',
    ]

    empresas_con_cluster_lyr = processing.run("native:joinattributestable", {
        'INPUT':               empresas_con_zona_raw,
        'FIELD':               'uid_poly',
        'INPUT_2':             clusteres_con_metricas,
        'FIELD_2':             'uid_poly',
        'FIELDS_TO_COPY':      METRICAS_CLUSTER,
        'METHOD':              1,
        'DISCARD_NONMATCHING': False,
        'OUTPUT':              'TEMPORARY_OUTPUT'
    })['OUTPUT']

    # 17c - eliminar campos geograficos del source antes del join
    #
    # El source tiene departamento/provincia/distrito (minusculas) con posibles NULLs.
    # El shapefile INEI tiene PROVINCIA y DISTRITO (mayusculas). QGIS trata nombres
    # de campo como case-insensitive en GPKG, por lo que si ambos coexisten en el join
    # los renombra a _2 silenciosamente. La solucion es eliminar los del source primero,
    # para que al hacer el join los campos del INEI entren solos y sin ambiguedad.
    log("  17c: eliminando campos geograficos del source...")
    campos_sin_geo = [f.name() for f in empresas_con_cluster_lyr.fields()
                      if f.name().lower() not in ('departamento', 'provincia', 'distrito')]
    empresas_sin_geo_source = processing.run("native:retainfields", {
        'INPUT':  empresas_con_cluster_lyr,
        'FIELDS': campos_sin_geo,
        'OUTPUT': 'TEMPORARY_OUTPUT'
    })['OUTPUT']

    # 17d - join espacial con distritos
    # Ahora PROVINCIA y DISTRITO del INEI entran sin conflicto.
    # Se obtiene UBIGEO, DEPARTAMEN, PROVINCIA, DISTRITO para cada punto
    # segun su ubicacion geografica — siempre completos, sin NULLs.
    log("  17d: enriqueciendo con datos distritales por ubicacion geografica...")
    empresas_con_distrito = processing.run("native:joinattributesbylocation", {
        'INPUT':               empresas_sin_geo_source,
        'PREDICATE':           [0],    # intersects
        'JOIN':                distritos_fixed,
        'JOIN_FIELDS':         ['UBIGEO', 'DEPARTAMEN', 'PROVINCIA', 'DISTRITO'],
        'METHOD':              1,
        'DISCARD_NONMATCHING': False,
        'PREFIX':              '',
        'OUTPUT':              'TEMPORARY_OUTPUT'
    })['OUTPUT']

    # 17e - refactorfields: renombrar y reordenar campos en la salida final
    # Define exactamente que campos salen y en que orden.
    # El orden es: ubigeo / departamento / provincia / distrito -> empresa -> zona -> metricas
    log("  17e: renombrando y ordenando campos de salida...")
    CAMPOS_SALIDA = [
        # ubicacion geografica del INEI (completa, sin NULLs)
        {'nombre': 'ubigeo',      'fuente': 'UBIGEO'},
        {'nombre': 'departamento','fuente': 'DEPARTAMEN'},
        {'nombre': 'provincia',   'fuente': 'PROVINCIA'},
        {'nombre': 'distrito',    'fuente': 'DISTRITO'},
        # identificacion y ventas de la empresa
        {'nombre': 'key_value',                     'fuente': 'key_value'},
        {'nombre': 'sector_ciiu',                   'fuente': 'sector_ciiu'},
        {'nombre': 'prediccion_con_factor_mensual', 'fuente': 'prediccion_con_factor_mensual'},
        {'nombre': 'prediccion_con_factor_anual',   'fuente': 'prediccion_con_factor_anual'},
        # zona comercial asignada
        {'nombre': 'zona_comercial', 'fuente': 'zona_comercial'},
        {'nombre': 'uid_poly',       'fuente': 'uid_poly'},
        # metricas del cluster
        {'nombre': 'cluster_empresas_count',    'fuente': 'prediccion_con_factor_mensual_count'},
        {'nombre': 'cluster_ventas_mens_total', 'fuente': 'prediccion_con_factor_mensual_sum'},
        {'nombre': 'cluster_ventas_mens_prom',  'fuente': 'prediccion_con_factor_mensual_mean'},
        {'nombre': 'cluster_ventas_anu_total',  'fuente': 'prediccion_con_factor_anual_sum'},
        {'nombre': 'cluster_ventas_anu_prom',   'fuente': 'prediccion_con_factor_anual_mean'},
    ]

    campos_disponibles_17e = {f.name() for f in empresas_con_distrito.fields()}
    mapping = []
    for col in CAMPOS_SALIDA:
        if col['fuente'] not in campos_disponibles_17e:
            log(f"  AVISO - campo fuente no encontrado: {col['fuente']}")
            continue
        campo_qgs = empresas_con_distrito.fields().field(col['fuente'])
        mapping.append({
            'name':      col['nombre'],
            'type':      campo_qgs.type(),
            'length':    campo_qgs.length(),
            'precision': campo_qgs.precision(),
            'expression': f'"{col["fuente"]}"',
        })

    empresas_renombradas = processing.run("native:refactorfields", {
        'INPUT':          empresas_con_distrito,
        'FIELDS_MAPPING': mapping,
        'OUTPUT':         'TEMPORARY_OUTPUT'
    })['OUTPUT']

    CAMPOS_CSV_FINAL = [col['nombre'] for col in CAMPOS_SALIDA
                        if col['fuente'] in campos_disponibles_17e]

    # 17f - guardar GPKG con geometria para QGIS
    log("  17f: guardando GPKG con geometria...")
    empresas_limpia_raw = processing.run("native:retainfields", {
        'INPUT':  empresas_renombradas,
        'FIELDS': CAMPOS_CSV_FINAL,
        'OUTPUT': 'TEMPORARY_OUTPUT'
    })['OUTPUT']

    capa_empresas = guardar(
        empresas_limpia_raw,
        "empresas_asignadas_a_zona_comercial",
        "empresas_asignadas_a_zona_comercial.gpkg"
    )

    aplicar_aliases(capa_empresas, ALIASES_COMUNES)

    # 17g - exportar CSV (sin geometria, solo los campos utiles)
    # Verificar que todos los campos existen (CAMPOS_CSV_FINAL se construyo en 17d)
    campos_disponibles = {f.name() for f in capa_empresas.fields()}
    campos_faltantes   = [campo for campo in CAMPOS_CSV_FINAL if campo not in campos_disponibles]
    if campos_faltantes:
        log(f"  AVISO - campos no encontrados: {campos_faltantes}")

    empresas_csv_path = os.path.join(OUTPUT_DIR, "empresas_asignadas_a_zona_comercial.csv")

    with open(empresas_csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS_CSV_FINAL)
        writer.writeheader()
        for feat in capa_empresas.getFeatures():
            fila = {campo: qgis_a_python(feat[campo]) if campo in campos_disponibles else None
                    for campo in CAMPOS_CSV_FINAL}
            writer.writerow(fila)

    log(f"  {capa_empresas.featureCount()} empresas exportadas")
    log(f"  CSV guardado en: {empresas_csv_path}")

    # ------------------------------------------------------------------
    # PASO 19 - Cargar ranking como tabla en QGIS
    # El CSV de ranking no tiene geometria, se carga como tabla de
    # atributos para poder consultarlo directamente desde el proyecto.
    # ------------------------------------------------------------------
    log("PASO 19: Cargando ranking como tabla en QGIS...")

    uri_ranking = (
        f"file:///{ranking_csv_path.replace(os.sep, '/')}"
        f"?delimiter=,&encoding=UTF-8&type=csv&geometryType=none"
    )
    capa_ranking = QgsVectorLayer(uri_ranking, "ranking_169_mas_todos_los_distritos", "delimitedtext")

    if capa_ranking.isValid():
        QgsProject.instance().addMapLayer(capa_ranking)
        log(f"  tabla ranking cargada ({capa_ranking.featureCount()} filas)")
    else:
        log("  AVISO - no se pudo cargar el ranking como tabla en QGIS")

    # ------------------------------------------------------------------
    # PASO 20 - Organizar capas en grupos
    #
    # Grupo "Resultados": capas de analisis listas para usar
    #   - 05_clusteres_con_metricas
    #   - 12_metricas_ventas_x_empresas_por_distrito
    #   - 13_metricas_standalone_por_distrito
    #   - empresas_asignadas_a_zona_comercial
    #   - ranking_169_mas_todos_los_distritos
    #
    # Grupo "Validacion": capas de control
    #   - 00_puntos_sin_cobertura_manual
    # ------------------------------------------------------------------
    log("PASO 20: Organizando capas en grupos...")

    root = QgsProject.instance().layerTreeRoot()

    grupo_resultados = root.insertGroup(0, "Resultados")
    grupo_validacion = root.insertGroup(1, "Validacion")

    mover_a_grupo(clusteres_final,     grupo_resultados)
    # metricas_dist (capa 12) queda en el proyecto sin grupo:
    # su informacion ya esta en el ranking CSV con mejor orden.
    mover_a_grupo(metricas_standalone, grupo_resultados)
    mover_a_grupo(capa_empresas,       grupo_resultados)
    mover_a_grupo(capa_ranking,        grupo_resultados)

    mover_a_grupo(puntos_fuera, grupo_validacion)

    log("  grupos creados")

    # ------------------------------------------------------------------
    # PASO 21 - Deseleccionar todas las capas
    # Limpia la seleccion de features en todas las capas y deja el
    # proyecto sin ninguna capa activa al terminar.
    # ------------------------------------------------------------------
    log("PASO 21: Limpiando seleccion de capas...")

    for layer in QgsProject.instance().mapLayers().values():
        if hasattr(layer, 'removeSelection'):
            layer.removeSelection()

    try:
        iface.setActiveLayer(None)
    except Exception:
        pass

    log("  listo")

    # ------------------------------------------------------------------
    # RESUMEN
    # ------------------------------------------------------------------
    log("")
    log("=" * 60)
    log("PIPELINE COMPLETADO")
    log("=" * 60)
    log(f"  Clusters detectados         : {dbscan_result['NUM_CLUSTERS']}")
    log(f"  Puntos con cluster          : {ptos_asignados.featureCount()}")
    log(f"  Zonas comerciales           : {clusteres_final.featureCount()}")
    log(f"  Distritos con metricas      : {metricas_dist.featureCount()}")
    log(f"  Puntos standalone           : {ptos_standalone.featureCount()}")
    log(f"  Distritos standalone        : {metricas_standalone.featureCount()}")
    log(f"  Puntos sin cobertura manual : {n_fuera}")
    log(f"  Empresas con zona asignada  : {capa_empresas.featureCount()}")
    log(f"  Distritos en ranking        : {len(top_169) + len(resto)}")
    log(f"  Outputs en: {OUTPUT_DIR}")
    log("")
    log("  CAPAS EN GRUPO 'Resultados':")
    log("  - 05_clusteres_con_metricas")
    log("    Zonas comerciales con ventas totales, promedio y numero")
    log("    de empresas. Tambien tiene el distrito y ubigeo asignado.")
    log("  - 12_metricas_ventas_x_empresas_por_distrito")
    log("    Metricas de todos los distritos del Peru usando el total")
    log("    de empresas del dataset (~834k puntos).")
    log("  - 13_metricas_standalone_por_distrito")
    log("    Igual que la 12, pero solo con empresas fuera de clusters.")
    log("    Util para ver cuanta actividad no esta en zonas densas.")
    log("  - empresas_asignadas_a_zona_comercial")
    log("    Cada empresa con su zona comercial, uid_poly y las metricas")
    log("    de ventas del cluster al que pertenece.")
    log("  - ranking_169_mas_todos_los_distritos (tabla, sin geometria)")
    log("    Ranking completo de ~1800 distritos. Los 169 prioritarios")
    log("    van primero en orden fijo, el resto por ventas desc.")
    log("")
    log("  CAPAS EN GRUPO 'Validacion':")
    log("  - 00_puntos_sin_cobertura_manual")

    if n_fuera > 0:
        log(f"    REVISAR: tiene {n_fuera} puntos.")
        log("    Son empresas que DBSCAN agrupo pero quedan fuera de")
        log("    todos los poligonos manuales. Abrir esta capa, ver donde")
        log("    estan esos puntos y evaluar si hay que ampliar o agregar")
        log("    un poligono en 04_clusteres_manuales.")
    else:
        log("    OK: esta vacia. Todos los puntos clusterizados tienen")
        log("    cobertura de poligono manual.")

    log("")
    log("  PENDIENTE MANUAL:")
    log("  - Google Labels: instalar el plugin desde")
    log("    QGIS -> Complementos -> Administrar e instalar complementos")
    log("    Buscar 'QuickMapServices' o 'HCMGIS' para agregar Google Maps")
    log("    como fondo. Con ese fondo activo se puede revisar las zonas")
    log("    comerciales sobre imagen satelital.")
    log("  CSVs en outputs_ibk/ (abrir en Excel o compartir):")
    log("  - zonas_comerciales_con_metricas.csv")
    log("    Cada zona comercial con ventas totales, promedio y n. de empresas.")
    log("  - standalone_metricas_por_distrito.csv")
    log("    Actividad comercial dispersa (fuera de clusters) por distrito.")
    log("  - empresas_asignadas_a_zona_comercial.csv")
    log("    Cada empresa con su zona y metricas del cluster.")
    log("  - ranking_169_mas_todos_los_distritos.csv")
    log("    Ranking distrital completo: 169 prioritarios + resto por ventas.")
    log("=" * 60)


ejecutar_pipeline()