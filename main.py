import os
from flask import Flask, request, jsonify
import requests
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MI_TELEGRAM_ID = os.environ.get("MI_TELEGRAM_ID")  # Tu ID numérico para seguridad

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

def enviar_mensaje_telegram(chat_id, texto, mostrar_botones=False):
    url = f"https://telegram.com{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": texto}
    
    # Esto dibuja los botones directamente en el chat del usuario de Telegram
    if mostrar_botones:
        payload["reply_markup"] = {
            "keyboard": [[{"text": "🟢 Encender Sirenas"}, {"text": "🔴 Apagar Sirenas"}]],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
    
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

# Mantenemos la ruta anterior para que Make/HTTP funcione si lo necesitas de respaldo
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

# NUEVA RUTA: Recibe las interacciones directas de tu Bot de Telegram
@app.route('/webhook-telegram', methods=['POST'])
def webhook_telegram():
    datos = request.get_json()
    
    if datos and "message" in datos:
        chat_id = str(datos["message"]["chat"]["id"])
        texto_recibido = datos["message"].get("text", "")
        
        # --- MURO DE SEGURIDAD ESTRICTO ---
        if chat_id != str(MI_TELEGRAM_ID):
            enviar_mensaje_telegram(chat_id, "❌ Acceso denegado. Este sistema está restringido.")
            return "OK", 200
            
        # --- LÓGICA DE RESPUESTA A COMANDOS ---
        if texto_recibido == "/start":
            enviar_mensaje_telegram(chat_id, "🇨🇷 Sistema de Sirenas de Costa Rica activo. Elige una opción:", mostrar_botones=True)
        elif texto_recibido == "🟢 Encender Sirenas":
            enviar_mensaje_telegram(chat_id, "⏳ Procesando encendido...")
            exito, msg = conectar_y_ejecutar_escena(ESCENA_ENCENDER)
            if exito:
                enviar_mensaje_telegram(chat_id, "✅ Sirenas ENCENDIDAS correctamente.")
            else:
                enviar_mensaje_telegram(chat_id, f"❌ Error en la nube Tuya: {msg}")
        elif texto_recibido == "🔴 Apagar Sirenas":
            enviar_mensaje_telegram(chat_id, "⏳ Procesando apagado...")
            exito, msg = conectar_y_ejecutar_escena(ESCENA_APAGAR)
            if exito:
                enviar_mensaje_telegram(chat_id, "✅ Sirenas APAGADAS correctamente.")
            else:
                enviar_mensaje_telegram(chat_id, f"❌ Error en la nube Tuya: {msg}")
                
    return "OK", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
