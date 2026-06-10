"""
Generador de Swagger 2.0 para compatibilidad con Power Apps / Copilot Studio.

Convierte el OpenAPI 3.x auto-generado por FastAPI a formato Swagger 2.0
que es el formato nativo requerido por Microsoft Power Platform.

Uso:
    python generate_swagger.py
"""

import json
import os
import copy


def _convertir_schema(schema: dict, definitions: dict) -> dict:
    """
    Convierte un schema OpenAPI 3.x a Swagger 2.0.
    Elimina constructos no soportados como anyOf, oneOf con null.
    """
    if not isinstance(schema, dict):
        return schema

    resultado = {}

    for key, value in schema.items():
        # $ref no necesita cambio de path (ambos usan #/definitions/ en v2
        # pero FastAPI genera #/components/schemas/)
        if key == "$ref":
            # Convertir #/components/schemas/X -> #/definitions/X
            resultado["$ref"] = value.replace(
                "#/components/schemas/", "#/definitions/"
            )
            continue

        # anyOf con null -> tomar el tipo no-null
        if key == "anyOf":
            tipos_no_null = [t for t in value if t.get("type") != "null"]
            if tipos_no_null:
                resultado.update(_convertir_schema(tipos_no_null[0], definitions))
            continue

        # oneOf -> mismo tratamiento
        if key == "oneOf":
            tipos_no_null = [t for t in value if t.get("type") != "null"]
            if tipos_no_null:
                resultado.update(_convertir_schema(tipos_no_null[0], definitions))
            continue

        # exclusiveMinimum / exclusiveMaximum: en v3.1 es un numero,
        # en v2 es un booleano + minimum/maximum separado
        if key == "exclusiveMinimum":
            if isinstance(value, (int, float)):
                resultado["minimum"] = value
                resultado["exclusiveMinimum"] = True
            else:
                resultado["exclusiveMinimum"] = value
            continue

        if key == "exclusiveMaximum":
            if isinstance(value, (int, float)):
                resultado["maximum"] = value
                resultado["exclusiveMaximum"] = True
            else:
                resultado["exclusiveMaximum"] = value
            continue

        # Recursion para objetos anidados
        if key == "properties":
            resultado["properties"] = {}
            for prop_name, prop_val in value.items():
                resultado["properties"][prop_name] = _convertir_schema(
                    prop_val, definitions
                )
            continue

        if key == "items":
            if isinstance(value, dict):
                resultado["items"] = _convertir_schema(value, definitions)
            else:
                resultado["items"] = value
            continue

        # Campos validos en ambas versiones
        resultado[key] = value

    return resultado


def _convertir_definitions(schemas: dict) -> dict:
    """Convierte todos los schemas de components a definitions de Swagger 2.0."""
    definitions = {}
    for name, schema in schemas.items():
        definitions[name] = _convertir_schema(schema, definitions)
    return definitions


def _convertir_path(path_item: dict, definitions: dict) -> dict:
    """Convierte un path item de OpenAPI 3.x a Swagger 2.0."""
    resultado = {}

    for method, operation in path_item.items():
        if method not in ("get", "post", "put", "delete", "patch", "options", "head"):
            continue

        op = {
            "operationId": operation.get("operationId", ""),
            "summary": operation.get("summary", ""),
            "description": operation.get("description", ""),
            "produces": ["application/json"],
            "consumes": ["application/json"],
            "parameters": [],
            "responses": {},
        }

        # Tags si existen
        if "tags" in operation:
            op["tags"] = operation["tags"]

        # Convertir requestBody -> parameters con in: body
        if "requestBody" in operation:
            rb = operation["requestBody"]
            content = rb.get("content", {})
            json_content = content.get("application/json", {})
            schema = json_content.get("schema", {})

            param = {
                "in": "body",
                "name": "body",
                "required": rb.get("required", True),
                "schema": _convertir_schema(schema, definitions),
            }
            # Agregar description si existe
            if "description" in rb:
                param["description"] = rb["description"]

            op["parameters"].append(param)

        # Convertir query/path parameters si existen
        if "parameters" in operation:
            for param in operation["parameters"]:
                p = copy.deepcopy(param)
                if "schema" in p:
                    schema_conv = _convertir_schema(p.pop("schema"), definitions)
                    p.update(schema_conv)
                op["parameters"].append(p)

        # Si no hay parametros, eliminar la key
        if not op["parameters"]:
            del op["parameters"]

        # Convertir responses
        for status_code, response in operation.get("responses", {}).items():
            resp = {"description": response.get("description", "")}
            content = response.get("content", {})
            json_content = content.get("application/json", {})
            if "schema" in json_content:
                resp["schema"] = _convertir_schema(
                    json_content["schema"], definitions
                )
            op["responses"][status_code] = resp

        resultado[method] = op

    return resultado


def generar_swagger_20(app=None):
    """
    Genera el archivo openapi_cem.json en formato Swagger 2.0.
    
    Args:
        app: Instancia de FastAPI. Si es None, importa desde Simulacion2.
    """
    if app is None:
        from Simulacion2 import app

    # Temporalmente habilitar openapi_url para generar el schema
    original_url = app.openapi_url
    app.openapi_url = "/openapi.json"
    app.openapi_schema = None  # Limpiar cache
    openapi_3 = app.openapi()
    app.openapi_url = original_url  # Restaurar
    app.openapi_schema = None

    # Servidor (localhost para desarrollo, cambiar en produccion)
    host = "localhost:8000"
    base_path = "/"
    schemes = ["http"]

    # Construir el documento Swagger 2.0
    swagger_doc = {
        "swagger": "2.0",
        "info": openapi_3.get("info", {}),
        "host": host,
        "basePath": base_path,
        "schemes": schemes,
        "consumes": ["application/json"],
        "produces": ["application/json"],
    }

    # Convertir definitions (schemas)
    schemas = openapi_3.get("components", {}).get("schemas", {})
    swagger_doc["definitions"] = _convertir_definitions(schemas)

    # Post-procesamiento: asegurar que toda propiedad tenga un tipo explícito
    # Power Apps rechaza propiedades sin "type" ni "$ref"
    for def_name, definition in swagger_doc["definitions"].items():
        props = definition.get("properties", {})
        for prop_name, prop_val in list(props.items()):
            if isinstance(prop_val, dict) and "type" not in prop_val and "$ref" not in prop_val:
                prop_val["type"] = "string"

    # Convertir paths
    swagger_doc["paths"] = {}
    for path, path_item in openapi_3.get("paths", {}).items():
        swagger_doc["paths"][path] = _convertir_path(path_item, swagger_doc["definitions"])

    # Guardar archivo
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "openapi_cem.json",
    )
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(swagger_doc, f, indent=2, ensure_ascii=False)

    print(f"[OK] Swagger 2.0 generado: {output_path}")
    print(f"     Endpoints: {len(swagger_doc['paths'])}")
    print(f"     Definitions: {len(swagger_doc['definitions'])}")

    return swagger_doc


if __name__ == "__main__":
    generar_swagger_20()
