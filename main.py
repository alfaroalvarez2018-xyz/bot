import os
from flask import Flask, request, jsonify
import tinytuya

app = Flask(__name__)

# Configuración basada en tus datos de Costa Rica y TuyaSmart
COUNTRY_CODE = "506"  
APP_NAME = "tuyasmart"  
ESCENA_ENCENDER = "Encender_Sirenas"
ESCENA_APAGAR = "Apagar_Sirenas"

# Las credenciales de inicio de sesión se leen desde Render por seguridad
TUYA_EMAIL = os.environ.get("TUYA_EMAIL")
TUYA_PASSWORD = os.environ.get("TUYA_PASSWORD")

def conectar_y_ejecutar_escena(nombre_escena):
    if not TUYA_EMAIL or not TUYA_PASSWORD:
        return False, "Faltan las credenciales TUYA_EMAIL o TUYA_PASSWORD en el servidor."

    try:
        # Se conecta a la nube de América (us) con inicio de sesión estándar de celular
        cloud = tinytuya.Cloud(apiRegion="us", apiKey="none", apiSecret="none")
        cloud.login(TUYA_EMAIL, TUYA_PASSWORD, COUNTRY_CODE, APP_NAME)
        
        # Descarga la lista de todas tus escenas Tap-To-Run
        escenas = cloud.getscenes()
        
        if not escenas or 'result' not in escenas:
            return False, "No se encontraron escenas o las credenciales de la app son incorrectas."
            
        # Busca el ID de la escena por su nombre exacto
        id_escena = None
        for escena in escenas['result']:
            if escena.get('name') == nombre_escena:
                id_escena = escena.get('id')
                break
                
        if not id_escena:
            return False, f"No se encontró la escena '{nombre_escena}' en tu aplicación."
            
        # Ejecuta la escena directamente en tu casa
        resultado = cloud.triggerscene(id_escena)
        return True, resultado
        
    except Exception as e:
        return False, str(e)

@app.route('/controlar', methods=['POST'])
def controlar():
    datos = request.get_json()
    if not datos or 'accion' not in datos:
        return jsonify({"error": "Falta el parámetro 'accion' en el JSON"}), 400
        
    accion = datos.get('accion').lower()
    
    if accion == "encender":
        exito, msg = conectar_y_ejecutar_escena(ESCENA_ENCENDER)
    elif accion == "apagar":
        exito, msg = conectar_y_ejecutar_escena(ESCENA_APAGAR)
    else:
        return jsonify({"error": "Acción no permitida. Usa 'encender' o 'apagar'"}), 400
        
    if exito:
        return jsonify({"status": "exito", "detalle": msg}), 200
    else:
        return jsonify({"status": "error", "detalle": msg}), 500

if __name__ == '__main__':
    # Puerto dinámico obligatorio para Render
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
