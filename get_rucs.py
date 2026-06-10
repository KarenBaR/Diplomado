import pandas as pd
df = pd.read_csv('base_cem_v3.csv', sep='|', encoding='latin-1', dtype=str)
df_filtrado = df[df['periodo_campania'] == '202606'].head(10)
for idx, row in df_filtrado.iterrows():
    print(f"{row['numeroruc']} - {row['descripcion_val']}")
