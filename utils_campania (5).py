## nativos
from datetime import datetime
import os
import sys
## terceros
import sagemaker
import boto3

## config
BASE_DIR = os.path.dirname(os.getcwd())
if BASE_DIR not in sys.path: sys.path.append(BASE_DIR)
proyecto = 'propension'
#tabla_universo = 'T_TEST_UNIVERSO_202102'
tabla_universo = 'HM_UNIVERSO_PROPENSION_DESEMBOLSO_BPE'
reciente_universo = 'MM_UNIVERSO_PROPENSION_DESEMBOLSO_BPE'
tabla_sow_bpe = 'HM_SOW_BPE'
tabla_sow_be = 'HM_SOW_BE'
esquema_vpc = 'disc_comercial'
grupo_vpc = 'ibk-discovery-comercial-work-group'
path_ = 's3://ibk-discovery-comercial-us-east-1-654654352211-data/discovery/comercial/'

#from UTILITARIO_CODE.utils import targets, dicc_seleccionadas
#normal = [1]
#target = targets[7]
#print("target ::::::::: ", target)
#clasif = 'normal' if len(normal) == 1 else 'all'
#sufijo = '{}_clasif_{}'.format(target.lower(), clasif)
#seleccionadas = dicc_seleccionadas[sufijo]

## SDF
now = datetime.now()
sess = sagemaker.session.Session()
s3 = boto3.resource('s3')
bucket = 'ibk-discovery-comercial-us-east-1-654654352211-data' #sess.default_bucket() 
region = boto3.Session().region_name
smclient = boto3.Session().client('sagemaker')


## path Docker
path_container_input = '/opt/ml/processing/input'
path_container_output = path_container_input.replace('input', 'output')
path_container_utils= path_container_input + '/utils'
path_container_universo = path_container_input + '/' + tabla_universo

uri_sunat_cleaned = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/SUNAT_CLEANED'
uri_rcc_cleaned = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/RCC_CLEANED'

path_sunat_cleaned = '/opt/ml/processing/input/SUNAT_CLEANED'
path_rcc_cleaned = '/opt/ml/processing/input/RCC_CLEANED'

uri_seleccion_vars = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/SELECCION'
path_seleccion_vars = '/opt/ml/processing/input/SELECCION'

uri_correlacion = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/SELECCION/train_correlation.csv'
uri_analisis_vars = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/SELECCION/seleccion_variables_target_acepta_vpconnect.csv'
uri_woe_vars = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/SELECCION/woe_target_acepta_vpconnect.json'


uri_base_sunat = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/BASE_SUNAT'
uri_athena_rcc_aceptacion_1 = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/athena_2/STAGE_HM_UNIVERSO_RCC_ACEPTACION_1'
uri_athena_rcc_aceptacion_2 = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/athena_2/STAGE_HM_UNIVERSO_RCC_ACEPTACION_2'
path_base_sunat = '/opt/ml/processing/input/BASE_SUNAT'
path_athena_rcc_aceptacion_1 = '/opt/ml/processing/input/STAGE_HM_UNIVERSO_RCC_ACEPTACION_1'
path_athena_rcc_aceptacion_2 = '/opt/ml/processing/input/STAGE_HM_UNIVERSO_RCC_ACEPTACION_2'


uri_pre_model = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/PRE_MODEL'

uri_train_model = uri_pre_model + '/train_model.csv'
uri_valid_model = uri_pre_model + '/valid_model.csv'
uri_test_model = uri_pre_model + '/test_no_model.csv'

uri_models = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_2023/MODELS'

target = 'target_acepta_vpconnect'
target_desembolso = 'target_desembolso_bpe'
uri_output_campania_all = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_campania_all'
uri_output_campania = 's3://sagemaker-us-east-1-058528764918/vpc_new/aceptacion/output_campania'

pre_select_old = [
      'tip_contribuyente_val_fix_encoder_target_desembolso_bpe',
     'tiempo_alta',
     'flg_canal_c',
     'sum_acepta_tlv_or_vpconnect_u6m',
     'promedio_edad_rrll',
     'ciiu_val_autocomplete_encoder_target_acepta_vpconnect',
     'flg_cta_neg',
     'producto_promedio_rrll',
     'saldo_prom_tot_activo_rrll',
     'cant_clientes_txs_retail',
     'sum_no_acepta_tlv_or_vpconnect_u6m',
     'minutos_ibk_ce_u6m',
     'nro_tlv_bpe_u6m',
    'entidad_prin_desc_encoder_target_desembolso_bpe',
     'sum_lo_pensara_tlv_or_vpconnect_u6m'
]





pre_select = [
      'tiempo_alta',
      'nro_meses_no_acepta_campana_u12m',
      'nro_meses_acepta_campana_u3m',
      'recencia_acepta_campana',
      'tip_contribuyente_val_fix_encoder_target_desembolso_bpe',
      'flg_canal_c',
      'promedio_edad_rrll',
      'producto_promedio_rrll',
      'diff_min_avg_tasa_piso_u6m',
      'nro_meses_flg_gestionado_bpe_sin_reciclado_u12m',
      'recencia_gestionado_estricto_sin_reciclado',
      'minutos_llamada_ce_u3m',
      'entidad_prin_desc_encoder_target_desembolso_bpe',
      'tendencia_nro_entidades_6m',
      'tend_deuda_sf_ult_trim_mnt',
      'tendencia_saldo_coloc_direct_vig_venc_6m',
      'ubigeo_val_encoder_target_acepta_vpconnect',
      'ciiu_val_autocomplete_encoder_target_desembolso_bpe',
      'campania_priorizada_encoder_target_desembolso_bpe',
      'max_cobert_garhipauto_over_coloc_direct_vig_y_venc_u12m',
      'nro_llamadas_lo_pensara_u3m',
      'saldo_prom_tot_tc_rrll',
      'saldo_coloc_direct_tc',
      'bucket_encoder_target_acepta_vpconnect',
      'nro_llamadas_tasa_elevada_u12m',
      'flg_barrido_tlv_bpe_u',
      'flg_cta_neg_u',
      'flg_clientes_u',
      'flg_retail_u',
      'saldo_coloc_direct_vig_cajas',
      'oferta_rr',
      'max_ratio_nro_telefono_nc_u12m',
      'variacio_min_tasa_piso_u3m_u6m',
      'flg_pj',
      'max_peso_llamada_u12m'
    ]