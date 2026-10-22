"""Bot de sirenas: Telegram + TuyaSmart por autorización QR.

NO requiere un proyecto Tuya Developer ni Access ID / Access Secret.
Utiliza tuya-device-sharing-sdk y el canal de autorización de la integración
Home Assistant de Tuya. No instala Home Assistant. El consentimiento en la app
puede aparecer identificado como Home Assistant. La aceptación del canal desde
este cliente depende de Tuya; no se ha validado con una cuenta real.

requirements.txt:
Flask==3.1.3
requests==2.34.2
tuya-device-sharing-sdk==0.2.15
qrcode[pil]==8.2
gunicorn==26.2.0

Render / Start Command:
  gunicorn main:app --workers 1 --threads 1 --timeout 120

Variables obligatorias en Render:
  TELEGRAM_TOKEN: token NUEVO de BotFather; no escribirlo en GitHub.
  MI_TELEGRAM_ID: ID numérico del usuario autorizado, sin @.
  TELEGRAM_WEBHOOK_SECRET: secreto aleatorio de 32 a 256 caracteres,
    solo letras ASCII, números, _ o -.
  TUYA_USER_CODE: TuyaSmart > Yo > Ajustes > Cuenta y seguridad > Código de usuario.
  PUBLIC_BASE_URL: URL HTTPS de tu servicio Render.

1. Con las variables configuradas, registrar el webhook una vez:
     python main.py configurar-webhook
   Si Render no ofrece Shell, también se puede registrar desde el equipo local
   con esas mismas variables de entorno; no requiere acceder a las sirenas.
2. En Telegram privado: /start y luego /vincular.
3. Abrir el QR en otra pantalla y escanearlo desde TuyaSmart (+ > Escanear).
   Leer y aceptar el permiso en la app. Después enviar /confirmar al bot.
4. Usar /escenas para comprobar nombres sin activar nada. Ambas escenas deben
   llamarse exactamente Encender_Sirenas y Apagar_Sirenas, o configurar las
   variables opcionales ESCENA_ENCENDER y ESCENA_APAGAR con sus nombres reales.

La vinculación, los tokens renovados y los updates se guardan en SQLite en
UPDATES_DB (por defecto /tmp/sirenas_updates.sqlite3). No se envían a GitHub.
Si Render borra el almacenamiento temporal al reiniciar o desplegar, será
necesario repetir /vincular. Para conservar la sesión, usar un disco persistente
y configurar UPDATES_DB con una ruta dentro de ese disco. No usar varias réplicas.

CONTROLAR_SECRET es opcional. Si no existe, /controlar queda deshabilitada.
Si existe, Make/HTTP debe enviar Authorization: Bearer <CONTROLAR_SECRET>.
No usa TUYA_EMAIL, TUYA_PASSWORD, TUYA_API_KEY, TUYA_API_SECRET ni TUYA_HOME_ID.
Ninguna orden se ejecuta automáticamente al vincular la cuenta.
"""

import hmac
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time

import requests
import qrcode
from tuya_sharing import LoginControl, Manager
from tuya_sharing.customerapi import SharingTokenListener
from flask import Flask, jsonify, request

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024


def valor(nombre, defecto=''):
    return os.environ.get(nombre, defecto).strip()


TELEGRAM_TOKEN = valor('TELEGRAM_TOKEN')
MI_TELEGRAM_ID = valor('MI_TELEGRAM_ID')
WEBHOOK_SECRET = valor('TELEGRAM_WEBHOOK_SECRET')
CONTROLAR_SECRET = valor('CONTROLAR_SECRET')
TUYA_USER_CODE = valor('TUYA_USER_CODE')
# Identificador público de la integración Home Assistant, no una clave privada.
TUYA_CLIENT_ID = 'HA_3y9q4ak7g4ephrvke'
TUYA_SCHEMA = 'haauthorize'
# El SDK puede incluir información sensible en errores de red.
logging.getLogger('tuya_sharing').setLevel(logging.CRITICAL)
ESCENA_ENCENDER = valor('ESCENA_ENCENDER', 'Encender_Sirenas')
ESCENA_APAGAR = valor('ESCENA_APAGAR', 'Apagar_Sirenas')
UPDATES_DB = valor('UPDATES_DB', '/tmp/sirenas_updates.sqlite3')


def telegram_configurado():
    return bool(TELEGRAM_TOKEN and MI_TELEGRAM_ID.isdigit()
                and re.fullmatch(r'[A-Za-z0-9_-]{32,256}', WEBHOOK_SECRET))


def llamar_telegram(metodo, payload):
    if not TELEGRAM_TOKEN:
        return False, 'Falta TELEGRAM_TOKEN en Render.'
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{metodo}'
    try:
        respuesta = requests.post(url, json=payload, timeout=(5, 15))
        datos = respuesta.json()
        if respuesta.ok and isinstance(datos, dict) and datos.get('ok') is True:
            return True, datos.get('result')
        # No imprimir URL ni excepciones de requests: pueden contener el token.
        descripcion = datos.get('description', 'Respuesta no válida') if isinstance(datos, dict) else 'Respuesta no válida'
        app.logger.error('Telegram rechazó %s (HTTP %s).', metodo, respuesta.status_code)
        return False, descripcion
    except (requests.RequestException, ValueError):
        app.logger.error('No se pudo completar la solicitud a Telegram: %s.', metodo)
        return False, 'No se pudo comunicar con Telegram.'


def enviar_mensaje_telegram(chat_id, texto, mostrar_botones=False):
    payload = {'chat_id': chat_id, 'text': texto}
    if mostrar_botones:
        payload['reply_markup'] = {
            'keyboard': [[{'text': '🟢 Encender Sirenas'}, {'text': '🔴 Apagar Sirenas'}]],
            'resize_keyboard': True,
            'one_time_keyboard': False,
        }
    return llamar_telegram('sendMessage', payload)


def guardar_estado(nombre, contenido):
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        conexion.execute('CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        conexion.execute('INSERT OR REPLACE INTO state VALUES (?, ?)',
                         (nombre, json.dumps(contenido)))


def leer_estado(nombre):
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        conexion.execute('CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        fila = conexion.execute('SELECT payload FROM state WHERE name = ?', (nombre,)).fetchone()
    return json.loads(fila[0]) if fila else None


class GuardarToken(SharingTokenListener):
    def update_token(self, token_info):
        sesion = leer_estado('sesion')
        if not sesion:
            raise RuntimeError('Falta la sesión de Tuya.')
        sesion['token_info'] = token_info
        guardar_estado('sesion', sesion)


def obtener_manager():
    sesion = leer_estado('sesion')
    if not sesion or sesion.get('user_code') != TUYA_USER_CODE:
        raise RuntimeError('Primero usá /vincular, escaneá el QR en TuyaSmart y enviá /confirmar.')
    return Manager(TUYA_CLIENT_ID, sesion['user_code'], sesion['terminal_id'],
                   sesion['endpoint'], sesion['token_info'], GuardarToken())


def obtener_escenas(manager):
    hogares = manager.home_repository.query_homes()
    if not hogares:
        raise RuntimeError('No se encontraron hogares autorizados en Tuya.')
    return manager.scene_repository.query_scenes([hogar.id for hogar in hogares])


def iniciar_vinculacion(chat_id):
    if not TUYA_USER_CODE:
        enviar_mensaje_telegram(chat_id, 'Falta TUYA_USER_CODE en Render. Buscalo en TuyaSmart > Yo > Ajustes > Cuenta y seguridad > Código de usuario.')
        return
    try:
        respuesta = LoginControl().qr_code(TUYA_CLIENT_ID, TUYA_SCHEMA, TUYA_USER_CODE)
        if respuesta.get('success') is not True:
            enviar_mensaje_telegram(chat_id, 'Tuya rechazó generar el QR. Revisá el código de usuario. Si persiste, este canal de autorización no está disponible para tu cuenta o este cliente.')
            return
        token = respuesta['result']['qrcode']
        guardar_estado('qr_pendiente', {'token': token, 'created': time.time()})
        imagen = io.BytesIO()
        qrcode.make('tuyaSmart--qrLogin?token=' + token).save(imagen, format='PNG')
        imagen.seek(0)
        # Subir el QR directamente a Telegram; no usar servicios externos de QR.
        resultado = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto',
            data={'chat_id': chat_id, 'caption': 'Abrí este QR en otra pantalla. En TuyaSmart: + > Escanear. Revisá el permiso (puede indicar Home Assistant) y confirmá en la app. Después escribí /confirmar. No compartás este QR.'},
            files={'photo': ('vincular.png', imagen.getvalue(), 'image/png')},
            timeout=(5, 20))
        cuerpo = resultado.json()
        if not resultado.ok or cuerpo.get('ok') is not True:
            enviar_mensaje_telegram(chat_id, 'No se pudo enviar el QR. Volvé a intentar /vincular.')
    except Exception:
        enviar_mensaje_telegram(chat_id, 'No se pudo generar o enviar el QR. Intentá /vincular nuevamente.')


def confirmar_vinculacion(chat_id):
    try:
        pendiente = leer_estado('qr_pendiente')
        if not pendiente or time.time() - pendiente['created'] > 300:
            enviar_mensaje_telegram(chat_id, 'Generá un QR nuevo con /vincular.')
            return
        exito, info = LoginControl().login_result(pendiente['token'], TUYA_CLIENT_ID, TUYA_USER_CODE)
        if not exito:
            enviar_mensaje_telegram(chat_id, 'Tuya todavía no confirmó el permiso. Escaneá el QR y aceptá en la app; después repetí /confirmar. Si expiró, usá /vincular.')
            return
        campos = ('t', 'uid', 'expire_time', 'access_token', 'refresh_token')
        sesion = {'user_code': TUYA_USER_CODE, 'terminal_id': info['terminal_id'],
                  'endpoint': info['endpoint'], 'token_info': {campo: info[campo] for campo in campos}}
        guardar_estado('sesion', sesion)
        guardar_estado('qr_pendiente', None)
        enviar_mensaje_telegram(chat_id, '✅ Cuenta vinculada. Usá /escenas para comprobar los nombres sin activar nada.', True)
    except Exception:
        enviar_mensaje_telegram(chat_id, 'No se pudo completar la vinculación. Intentá nuevamente con /vincular.')


def listar_escenas(chat_id):
    try:
        escenas = obtener_escenas(obtener_manager())
        nombres = '\n'.join('- ' + escena.name for escena in escenas) or '(ninguna)'
        texto = 'Escenas disponibles (no se ejecutó ninguna):\n' + nombres
        for inicio in range(0, len(texto), 3500):
            enviar_mensaje_telegram(chat_id, texto[inicio:inicio + 3500])
    except RuntimeError as exc:
        enviar_mensaje_telegram(chat_id, str(exc))
    except Exception:
        enviar_mensaje_telegram(chat_id, 'No se pudieron consultar las escenas. Revisá la vinculación con /vincular.')


def conectar_y_ejecutar_escena(nombre_escena):
    try:
        manager = obtener_manager()
        escenas = obtener_escenas(manager)
        coincidencias = [e for e in escenas if e.name == nombre_escena]
        if len(coincidencias) != 1:
            return False, f'Debe existir una sola escena llamada {nombre_escena}. Usá /escenas para revisar los nombres.'
        escena = coincidencias[0]
        # SceneRepository devuelve la respuesta; Manager.trigger_scene la descarta.
        resultado = manager.scene_repository.trigger_scene(escena.home_id, escena.scene_id)
        if resultado is not True:
            return False, 'Tuya no confirmó la ejecución. Verificá el estado antes de repetir la orden.'
        return True, 'Tuya aceptó ejecutar la escena.'
    except RuntimeError as exc:
        return False, str(exc)
    except Exception:
        app.logger.error('Falló la operación con Tuya; no se registran credenciales.')
        return False, 'No se pudo confirmar la orden con Tuya. Verificá el estado de las sirenas antes de repetirla. Si la sesión venció, usá /vincular.'


def reservar_update(update_id):
    """Evita repetir una orden si Telegram reenvía un update ya recibido.

    Se registra antes de ejecutar: un fallo posterior requiere una orden nueva.
    La protección dura mientras se conserve esta base de datos en esta instancia.
    """
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        conexion.execute('CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, fecha REAL NOT NULL)')
        resultado = conexion.execute('INSERT OR IGNORE INTO updates VALUES (?, ?)', (update_id, time.time()))
        return resultado.rowcount == 1


@app.route('/', methods=['GET'])
def estado():
    return jsonify({'status': 'activo'})


@app.route('/controlar', methods=['POST'])
def controlar():
    # Deshabilitada si no se configura el secreto de Make/HTTP.
    if not CONTROLAR_SECRET:
        return jsonify({'error': 'Ruta deshabilitada.'}), 403
    esperado = ('Bearer ' + CONTROLAR_SECRET).encode('utf-8')
    recibido = request.headers.get('Authorization', '').encode('utf-8')
    if not hmac.compare_digest(recibido, esperado):
        return jsonify({'error': 'Acceso denegado.'}), 403
    datos = request.get_json(silent=True)
    accion = datos.get('accion') if isinstance(datos, dict) else None
    if not isinstance(accion, str) or accion.strip().lower() not in ('encender', 'apagar'):
        return jsonify({'error': 'Usa un JSON con accion: encender o apagar.'}), 400
    nombre = ESCENA_ENCENDER if accion.strip().lower() == 'encender' else ESCENA_APAGAR
    exito, mensaje = conectar_y_ejecutar_escena(nombre)
    return jsonify({'status': 'exito' if exito else 'error', 'detalle': mensaje}), 200 if exito else 502


@app.route('/webhook-telegram', methods=['POST'])
def webhook_telegram():
    if not telegram_configurado():
        return jsonify({'error': 'Revisá TELEGRAM_TOKEN, MI_TELEGRAM_ID y TELEGRAM_WEBHOOK_SECRET.'}), 503
    recibido = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not hmac.compare_digest(recibido.encode('utf-8'), WEBHOOK_SECRET.encode('utf-8')):
        return 'Acceso denegado', 403
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return 'JSON no válido', 400
    mensaje = datos.get('message')
    if not isinstance(mensaje, dict):
        return 'OK', 200
    chat = mensaje.get('chat', {})
    remitente = mensaje.get('from', {})
    # Autoriza al remitente y exige chat privado, no un grupo compartido.
    if str(remitente.get('id')) != MI_TELEGRAM_ID or chat.get('type') != 'private':
        return 'OK', 200
    chat_id = chat.get('id')
    texto = mensaje.get('text', '')
    if chat_id is None or not isinstance(texto, str):
        return 'OK', 200
    update_id = datos.get('update_id')
    if type(update_id) is not int:
        return 'Falta update_id', 400
    try:
        if not reservar_update(update_id):
            return 'OK', 200
    except sqlite3.Error:
        app.logger.error('No se pudo registrar update_id; no se ejecutó la orden.')
        return 'Error temporal', 503
    texto = texto.strip()
    if texto == '/start':
        enviar_mensaje_telegram(chat_id, '🇨🇷 Control de sirenas. Primero usá /vincular y /confirmar para autorizar TuyaSmart. /escenas muestra los nombres sin activar nada.', True)
    elif texto == '/vincular':
        iniciar_vinculacion(chat_id)
    elif texto == '/confirmar':
        confirmar_vinculacion(chat_id)
    elif texto == '/escenas':
        listar_escenas(chat_id)
    elif texto in ('🟢 Encender Sirenas', '🔴 Apagar Sirenas'):
        encender = texto == '🟢 Encender Sirenas'
        enviar_mensaje_telegram(chat_id, '⏳ Procesando la solicitud...')
        exito, detalle = conectar_y_ejecutar_escena(ESCENA_ENCENDER if encender else ESCENA_APAGAR)
        if exito:
            accion = 'encendido' if encender else 'apagado'
            enviar_mensaje_telegram(chat_id, f'✅ Tuya aceptó la orden de {accion}. Verificá el estado de las sirenas.')
        else:
            enviar_mensaje_telegram(chat_id, f'❌ {detalle}')
    else:
        enviar_mensaje_telegram(chat_id, 'Usá los botones para elegir una acción.', True)
    return 'OK', 200


def configurar_webhook():
    if not telegram_configurado():
        raise SystemExit('Configurá TELEGRAM_TOKEN, MI_TELEGRAM_ID y un TELEGRAM_WEBHOOK_SECRET de 32 a 256 caracteres (letras, números, _ o -).')
    base = valor('PUBLIC_BASE_URL').rstrip('/')
    if not base.startswith('https://'):
        raise SystemExit('PUBLIC_BASE_URL debe ser la URL HTTPS de tu servicio Render.')
    exito, _ = llamar_telegram('setWebhook', {
        'url': base + '/webhook-telegram',
        'secret_token': WEBHOOK_SECRET,
        'allowed_updates': ['message'],
        'max_connections': 1,
        # Descarta órdenes anteriores para que no se ejecuten al reconectar.
        'drop_pending_updates': True,
    })
    if not exito:
        raise SystemExit('No se pudo registrar el webhook. Revisá el token y la URL.')
    print('Webhook registrado. Enviá /start al bot en un chat privado.')


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'configurar-webhook':
        configurar_webhook()
    else:
        app.run(host='0.0.0.0', port=int(valor('PORT', '8080')))
