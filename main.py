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
cryptography

Render / Start Command:
  gunicorn main:app --workers 1 --threads 1 --timeout 120

Variables obligatorias en Render:
  TELEGRAM_TOKEN: token NUEVO de BotFather; no escribirlo en GitHub.
  MI_TELEGRAM_ID: tu ID numérico; sos la persona administradora del bot.
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
TELEFONOS_AUTORIZADOS: lista de teléfonos internacionales separados por comas,
por ejemplo +50688888888,+50687777777 (números ficticios). Incluir código de país.
Cada persona abre el bot, envía /start y pulsa Compartir mi teléfono. El bot
comprueba que el contacto pertenece a quien escribe y que su teléfono está en
la lista. La vinculación teléfono-ID se guarda en SQLite; se consulta la lista
en cada orden. Para revocar acceso, eliminar el teléfono y volver a desplegar.
Si Render borra SQLite, las personas deben compartir su teléfono otra vez.
MI_TELEGRAM_ID siempre autoriza a la persona administradora, incluso sin lista.
Solo la administración puede usar /vincular, /confirmar y /escenas.
TELEGRAM_IDS_AUTORIZADOS ya no se utiliza; no permite saltarse la verificación.
No usa TUYA_EMAIL, TUYA_PASSWORD, TUYA_API_KEY, TUYA_API_SECRET ni TUYA_HOME_ID.
Ninguna orden se ejecuta automáticamente al vincular la cuenta.
REGISTRO_USUARIOS_JSON: objeto JSON privado indexado por teléfono internacional.
Cada ficha: {"nombre": "María — Casa 12", "latitud": 10.0, "longitud": -84.0}.
Solo es necesario el nombre. Las coordenadas antiguas se aceptan, pero no se usan.
El mapa se obtiene exclusivamente del punto que la persona comparta en Telegram.
La clave "admin" configura nombre/casa de la administración (no necesita contacto).
Conservar TELEFONOS_AUTORIZADOS; agregar una ficha NO autoriza por sí solo.
Tras un encendido aceptado por Tuya, se envía el aviso de inmediato al admin
más las personas vinculadas que siguen autorizadas. Después se ofrece compartir
la ubicación actual del teléfono de forma opcional durante 3 minutos.
Compartir o rechazar la ubicación nunca ejecuta ni repite una escena.
El mapa llega como complemento silencioso identificado por nombre y hora.
Se rechazan ubicaciones reenviadas, continuas, vencidas o sin encendido pendiente.
Telegram entrega un punto compartido, sin prueba criptográfica de su origen GPS.
Al apagar se cierran todas las solicitudes pendientes; salir cierra la propia.
La difusión corre en un hilo con cola SQLite, hasta tres intentos por destinatario,
y descarta avisos de más de cinco minutos. Un timeout de Telegram puede duplicar
un aviso, nunca repite la orden a Tuya. La entrega no está garantizada si bloquean
el bot, no hay red, Render se duerme o se borra su disco temporal. Esta versión
no incorpora aún una base de datos externa. No enviar teléfonos/coordenadas a GitHub.
Los controles se muestran en dos filas, con estilos verde y rojo, y teclado de
altura completa. El tamaño final y los colores dependen del cliente Telegram.
"""

import hmac
import io
import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
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


def normalizar_telefono(numero):
    """Formato internacional sin +; nunca adivinar un código de país."""
    if not isinstance(numero, str):
        return None
    numero = numero.strip()
    if not re.fullmatch(r'\+?[0-9 ()-]+', numero):
        return None
    digitos = re.sub(r'[ ()-]', '', numero).removeprefix('+')
    return digitos if re.fullmatch(r'[1-9][0-9]{7,14}', digitos) else None


TELEFONOS_CONFIG = [numero.strip()
                    for numero in valor('TELEFONOS_AUTORIZADOS').split(',')
                    if numero.strip()]
TELEFONOS_NORMALIZADOS = [normalizar_telefono(numero) for numero in TELEFONOS_CONFIG]
TELEFONOS_CONFIG_VALIDOS = all(numero is not None for numero in TELEFONOS_NORMALIZADOS)
TELEFONOS_AUTORIZADOS = {numero for numero in TELEFONOS_NORMALIZADOS if numero}
# Registro privado en Render. No otorga permisos: se conserva la lista existente.
# La clave "admin" permite asignar nombre y casa a MI_TELEGRAM_ID.
def cargar_registro():
    try:
        datos = json.loads(valor('REGISTRO_USUARIOS_JSON', '{}'))
        if not isinstance(datos, dict):
            raise ValueError
        registro = {}
        for clave, ficha in datos.items():
            telefono = 'admin' if clave == 'admin' else normalizar_telefono(clave)
            if not telefono or telefono in registro or not isinstance(ficha, dict):
                raise ValueError
            nombre = ficha.get('nombre')
            if not isinstance(nombre, str) or not nombre.strip() or len(nombre) > 120:
                raise ValueError
            if any(ord(c) < 32 for c in nombre):
                raise ValueError
            punto = {}
            if 'latitud' in ficha or 'longitud' in ficha:
                for campo, limite in (('latitud', 90), ('longitud', 180)):
                    n = ficha.get(campo)
                    if (type(n) not in (int, float) or not math.isfinite(n)
                            or not -limite <= n <= limite):
                        raise ValueError
                    punto[campo] = n
            registro[telefono] = {'nombre': nombre.strip(), **punto}
        return registro
    except (ValueError, TypeError):
        # Nunca incluir teléfonos ni el contenido de la variable en los logs.
        raise ValueError('Revisá REGISTRO_USUARIOS_JSON: JSON con nombres y pares de coordenadas válidos.') from None


REGISTRO_USUARIOS = cargar_registro()

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
    return bool(TELEGRAM_TOKEN and re.fullmatch(r'[1-9][0-9]*', MI_TELEGRAM_ID)
                and TELEFONOS_CONFIG_VALIDOS
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
            'keyboard': [
                [{'text': '🟢 ENCENDER SIRENAS', 'style': 'success'}],
                [{'text': '🔴 APAGAR SIRENAS', 'style': 'danger'}],
            ],
            'resize_keyboard': False,
            'one_time_keyboard': False,
        }
    if mostrar_botones and str(chat_id) != MI_TELEGRAM_ID:
        payload['reply_markup']['keyboard'].append([{'text': '🚪 Desvincularme'}])
    return llamar_telegram('sendMessage', payload)


def solicitar_telefono(chat_id, texto=None):
    return llamar_telegram('sendMessage', {
        'chat_id': chat_id,
        'text': texto or ('Para ingresar, pulsá 📱 Vincularme y aceptá compartir '
                         'el teléfono de tu cuenta de Telegram. El bot verificará '
                         'tu número contra la lista autorizada y guardará su vínculo '
                         'con tu cuenta. Al encender, tu nombre asignado se compartirá con las personas autorizadas vinculadas. Después podrás compartir tu ubicación de forma opcional. Si el botón está oculto, tocá el ícono de '
                         'teclado o cuadrícula junto al campo de mensajes.'),
        'reply_markup': {
            'keyboard': [[{'text': '📱 Vincularme', 'request_contact': True}]],
            'resize_keyboard': True,
            'one_time_keyboard': False,
            'is_persistent': True,
        },
    })


def crear_tabla_telefonos(conexion):
    conexion.execute('''CREATE TABLE IF NOT EXISTS authorized_phones (
        user_id TEXT PRIMARY KEY,
        phone TEXT NOT NULL UNIQUE,
        registered REAL NOT NULL
    )''')


def registrar_telefono(usuario_id, telefono):
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_telefonos(conexion)
        # Si el número ahora pertenece a otra cuenta, retirar el vínculo anterior.
        conexion.execute('DELETE FROM authorized_phones WHERE phone = ? AND user_id != ?',
                         (telefono, usuario_id))
        conexion.execute('INSERT OR REPLACE INTO authorized_phones VALUES (?, ?, ?)',
                         (usuario_id, telefono, time.time()))


def quitar_registro_telefono(usuario_id):
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_telefonos(conexion)
        conexion.execute('DELETE FROM authorized_phones WHERE user_id = ?', (usuario_id,))


def salir_del_bot(chat_id, usuario_id):
    if usuario_id == MI_TELEGRAM_ID:
        enviar_mensaje_telegram(
            chat_id,
            'Tu cuenta es la administradora y conserva su acceso por ID. '
            'La opción de salida corresponde a los usuarios regulares.',
            True,
        )
        return
    quitar_registro_telefono(usuario_id)
    descartar_ubicacion(usuario_id)
    llamar_telegram('sendMessage', {
        'chat_id': chat_id,
        'text': ('✅ Saliste del bot. Se eliminó el vínculo entre tu teléfono '
                 'y tu cuenta de Telegram y se retiraron los controles de las sirenas. '
                 'Para volver, pulsá 📱 Vincularme y compartí tu teléfono otra vez. '
                 'Podrás ingresar si tu número sigue en la lista autorizada.'),
        'reply_markup': {
            'keyboard': [[{'text': '📱 Vincularme', 'request_contact': True}]],
            'resize_keyboard': True,
            'one_time_keyboard': False,
            'is_persistent': True,
        },
    })


def usuario_autorizado(usuario_id):
    if usuario_id == MI_TELEGRAM_ID:
        return True
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_telefonos(conexion)
        fila = conexion.execute('SELECT phone FROM authorized_phones WHERE user_id = ?',
                                (usuario_id,)).fetchone()
    # Haber recibido botones o estar registrado no basta: revisar la lista actual.
    return bool(fila and fila[0] in TELEFONOS_AUTORIZADOS)


def ficha_usuario(usuario_id):
    if usuario_id == MI_TELEGRAM_ID:
        return REGISTRO_USUARIOS.get('admin', {'nombre': 'La administración'})
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_telefonos(conexion)
        fila = conexion.execute('SELECT phone FROM authorized_phones WHERE user_id = ?',
                                (usuario_id,)).fetchone()
    # No recurrir al nombre de Telegram, ni exponer el teléfono como alternativa.
    return REGISTRO_USUARIOS.get(fila[0], {'nombre': 'Una persona autorizada'}) if fila else {'nombre': 'Una persona autorizada'}


def crear_tabla_avisos(conexion):
    conexion.execute("""CREATE TABLE IF NOT EXISTS pending_alerts (
        id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL,
        user_id TEXT NOT NULL, phone TEXT, payload TEXT NOT NULL,
        created REAL NOT NULL, next_attempt REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        UNIQUE(event_id, user_id)
    )""")


_worker_lock = threading.Lock()
_worker_thread = None


def encolar_para_autorizados(conexion, event_id, payload):
    ahora = time.time()
    crear_tabla_telefonos(conexion)
    crear_tabla_avisos(conexion)
    destinatarios = {MI_TELEGRAM_ID: None}
    for uid, telefono in conexion.execute('SELECT user_id, phone FROM authorized_phones'):
        if telefono in TELEFONOS_AUTORIZADOS and uid != MI_TELEGRAM_ID:
            destinatarios[uid] = telefono
    for uid, telefono in destinatarios.items():
        conexion.execute("""INSERT OR IGNORE INTO pending_alerts
            (event_id, user_id, phone, payload, created, next_attempt)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, uid, telefono, json.dumps(payload), ahora, ahora))


def fecha_costa_rica(instante):
    return datetime.fromtimestamp(instante, timezone(timedelta(hours=-6))).strftime('%d/%m/%Y %H:%M:%S')


def encolar_aviso_encendido(usuario_id, update_id):
    ficha = ficha_usuario(usuario_id)
    fecha = fecha_costa_rica(time.time())
    texto = (f'🚨 {ficha["nombre"]} envió la orden de encender las sirenas, '
             'revisa el "Grupo Organizado Trinidad" en WhatsApp.'
             f'\n\n🕒 {fecha} (Costa Rica).'
             '\nConfirma que las sirenas estén sonando.')
    # Este aviso sale sin esperar ubicación y nunca incluye la antigua casa fija.
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        encolar_para_autorizados(conexion, update_id, {'text': texto, 'disable_notification': False})
    iniciar_worker_avisos()


def crear_tabla_ubicaciones(conexion):
    conexion.execute("""CREATE TABLE IF NOT EXISTS pending_locations (
        user_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL,
        phone TEXT, nombre TEXT NOT NULL, created REAL NOT NULL
    )""")


def descartar_ubicacion(usuario_id=None):
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_ubicaciones(conexion)
        if usuario_id is None:
            conexion.execute('DELETE FROM pending_locations')
        else:
            conexion.execute('DELETE FROM pending_locations WHERE user_id = ?', (usuario_id,))


def solicitar_ubicacion_opcional(chat_id, usuario_id, update_id):
    ficha = ficha_usuario(usuario_id)
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_ubicaciones(conexion)
        crear_tabla_telefonos(conexion)
        fila = conexion.execute('SELECT phone FROM authorized_phones WHERE user_id = ?',
                                (usuario_id,)).fetchone()
        telefono = fila[0] if fila and usuario_id != MI_TELEGRAM_ID else None
        conexion.execute('INSERT OR REPLACE INTO pending_locations VALUES (?, ?, ?, ?, ?)',
                         (usuario_id, update_id, telefono, ficha['nombre'], time.time()))
    llamar_telegram('sendMessage', {
        'chat_id': chat_id,
        'text': ('📍 Compartir tu ubicación es opcional. La orden de encendido ya fue '
                 'aceptada por Tuya y el aviso se está enviando. Si querés agregar '
                 'el punto donde estás, pulsá el botón y aceptá compartir tu ubicación '
                 'actual en Telegram. Se enviará a la administración y a las personas '
                 'vinculadas y autorizadas. Tenés 3 minutos. También podés continuar '
                 'sin ubicación; esto no cambia la orden enviada.'),
        'reply_markup': {
            'keyboard': [
                [{'text': '📍 Compartir Ubicación (Recomendado)', 'request_location': True}],
                [{'text': '⏭️ Continuar sin ubicación'}],
                [{'text': '🔴 APAGAR SIRENAS', 'style': 'danger'}],
            ],
            'resize_keyboard': False, 'one_time_keyboard': False, 'is_persistent': True,
        },
    })


def procesar_ubicacion(chat_id, usuario_id, mensaje):
    """Solo agrega un mapa a un encendido reciente. Nunca ejecuta escenas."""
    punto = mensaje.get('location')
    if (not isinstance(punto, dict) or mensaje.get('forward_origin') is not None
            or mensaje.get('forward_date') is not None or punto.get('live_period') is not None):
        enviar_mensaje_telegram(chat_id, 'Usá el botón para compartir una ubicación puntual, sin reenviar mensajes. Compartirla es opcional y no modifica las sirenas.')
        return
    lat, lon = punto.get('latitude'), punto.get('longitude')
    if any(type(n) not in (int, float) or not math.isfinite(n) or not -limite <= n <= limite
           for n, limite in ((lat, 90), (lon, 180))):
        enviar_mensaje_telegram(chat_id, 'No se pudo leer el punto. Podés continuar sin ubicación; la orden enviada no cambia.')
        return
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_ubicaciones(conexion)
        crear_tabla_telefonos(conexion)
        # Consumo y cola en una transacción: un punto no se difunde dos veces.
        conexion.execute('BEGIN IMMEDIATE')
        fila = conexion.execute('SELECT event_id, phone, nombre, created FROM pending_locations WHERE user_id = ?',
                                (usuario_id,)).fetchone()
        activo = bool(fila and 0 <= time.time() - fila[3] <= 180)
        if activo and usuario_id != MI_TELEGRAM_ID:
            vinculo = conexion.execute('SELECT phone FROM authorized_phones WHERE user_id = ?', (usuario_id,)).fetchone()
            activo = bool(vinculo and vinculo[0] == fila[1] and fila[1] in TELEFONOS_AUTORIZADOS)
        if activo:
            event_id, _, nombre, fecha = fila
            texto = (f'📍 Ubicación compartida por {nombre} para su orden de encendido '
                     f'del {fecha_costa_rica(fecha)} (Costa Rica).'
                     '\nEs el punto que compartió en Telegram; puede ser distinto de su casa.'
                     '\nEste mensaje no envía otra orden a las sirenas.')
            payload = {
                'text': texto, 'disable_notification': True,
                'reply_markup': {'inline_keyboard': [[{
                    'text': '📍 Abrir ubicación en Google Maps',
                    'url': f'https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}',
                }]]},
                'punto': {'latitude': lat, 'longitude': lon},
            }
            # IDs de encendido no negativos; negativos separan su complemento de mapa.
            encolar_para_autorizados(conexion, -event_id - 1, payload)
        conexion.execute('DELETE FROM pending_locations WHERE user_id = ?', (usuario_id,))
    if activo:
        iniciar_worker_avisos()
        enviar_mensaje_telegram(chat_id, '✅ Tu ubicación quedó preparada para enviarse a las personas autorizadas. No se ejecutó otra orden.', True)
    else:
        enviar_mensaje_telegram(chat_id, 'No hay una solicitud de ubicación vigente. Las solicitudes duran 3 minutos y se cierran al apagar o continuar sin ubicación. Este punto no se compartió y no cambió las sirenas.', True)


def entregar_aviso_pendiente():
    """Un intento por llamada; los avisos nunca vuelven a ejecutar una escena."""
    ahora = time.time()
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        crear_tabla_avisos(conexion)
        # No repartir alertas antiguas como si acabaran de ocurrir.
        conexion.execute('DELETE FROM pending_alerts WHERE created < ?', (ahora - 300,))
        fila = conexion.execute("""SELECT id, user_id, phone, payload, attempts
            FROM pending_alerts WHERE next_attempt <= ? ORDER BY id LIMIT 1""", (ahora,)).fetchone()
    if not fila:
        return False
    aviso_id, uid, telefono, contenido, intentos = fila
    autorizado = uid == MI_TELEGRAM_ID
    if not autorizado and telefono in TELEFONOS_AUTORIZADOS:
        with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
            crear_tabla_telefonos(conexion)
            vinculo = conexion.execute('SELECT phone FROM authorized_phones WHERE user_id = ?', (uid,)).fetchone()
        autorizado = bool(vinculo and vinculo[0] == telefono)
    payload = json.loads(contenido)
    punto = payload.pop('punto', None)
    exito = True
    if autorizado:
        exito, _ = llamar_telegram('sendMessage', {'chat_id': uid, **payload})
        if exito and punto:
            # El texto ya tiene enlace al punto si esta segunda llamada falla.
            llamar_telegram('sendLocation', {'chat_id': uid, **punto, 'disable_notification': True})
    with sqlite3.connect(UPDATES_DB, timeout=5) as conexion:
        if exito or intentos >= 2:
            conexion.execute('DELETE FROM pending_alerts WHERE id = ?', (aviso_id,))
            if not exito:
                app.logger.warning('No se pudo entregar un aviso después de tres intentos.')
        else:
            conexion.execute('UPDATE pending_alerts SET attempts = attempts + 1, next_attempt = ? WHERE id = ?',
                             (time.time() + 30 * (intentos + 1), aviso_id))
    return True


def worker_avisos():
    while True:
        try:
            enviado = entregar_aviso_pendiente()
        except Exception:
            app.logger.error('Error temporal al repartir avisos; no se repiten órdenes a Tuya.')
            enviado = False
        time.sleep(0.12 if enviado else 5)


def iniciar_worker_avisos():
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=worker_avisos, daemon=True, name='avisos-sirenas')
            _worker_thread.start()


def procesar_contacto(chat_id, usuario_id, mensaje):
    contacto = mensaje.get('contact')
    if (not isinstance(contacto, dict)
            or str(contacto.get('user_id')) != usuario_id
            or mensaje.get('forward_origin') is not None
            or mensaje.get('forward_date') is not None):
        solicitar_telefono(chat_id, 'Compartí tu propio número usando el botón de abajo. No se aceptan contactos de otras personas ni mensajes reenviados.')
        return
    telefono = normalizar_telefono(contacto.get('phone_number'))
    if not telefono:
        solicitar_telefono(chat_id, 'No se pudo comprobar el número. Usá el botón para compartir tu propio teléfono.')
        return
    if telefono not in TELEFONOS_AUTORIZADOS:
        quitar_registro_telefono(usuario_id)
        solicitar_telefono(chat_id, 'Tu teléfono no está en la lista de personas autorizadas. Consultá con la persona administradora.')
        return
    registrar_telefono(usuario_id, telefono)
    enviar_mensaje_telegram(chat_id, '✅ Teléfono verificado. Ya podés usar los botones para encender o apagar las sirenas.', True)


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
        return jsonify({'error': 'Revisá TELEGRAM_TOKEN, MI_TELEGRAM_ID, TELEGRAM_WEBHOOK_SECRET y TELEFONOS_AUTORIZADOS (teléfonos internacionales separados por comas).'}), 503
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
    if not isinstance(chat, dict) or not isinstance(remitente, dict):
        return 'OK', 200
    remitente_id = str(remitente.get('id'))
    chat_id = chat.get('id')
    if (chat.get('type') != 'private' or str(chat_id) != remitente_id
            or not re.fullmatch(r'[1-9][0-9]*', remitente_id)
            or remitente.get('is_bot') is True):
        return 'OK', 200
    update_id = datos.get('update_id')
    if type(update_id) is not int or update_id < 0:
        return 'Falta update_id', 400
    try:
        if not reservar_update(update_id):
            return 'OK', 200
        texto_salida = mensaje.get('text', '')
        if (isinstance(texto_salida, str)
                and texto_salida.strip().casefold() in ('/salir', 'salir', '🚪 salir del bot', '🚪 desvincularme', 'desvincularme')):
            # Eliminar el vínculo incluso si ya se retiró su número de la lista.
            salir_del_bot(chat_id, remitente_id)
            return 'OK', 200
        if 'contact' in mensaje and remitente_id != MI_TELEGRAM_ID:
            procesar_contacto(chat_id, remitente_id, mensaje)
            return 'OK', 200
        if not usuario_autorizado(remitente_id):
            descartar_ubicacion(remitente_id)
            solicitar_telefono(chat_id)
            return 'OK', 200
        if 'location' in mensaje:
            procesar_ubicacion(chat_id, remitente_id, mensaje)
            return 'OK', 200
    except sqlite3.Error:
        app.logger.error('Error al comprobar acceso; no se ejecutó ninguna orden.')
        return 'Error temporal', 503
    texto = mensaje.get('text', '')
    if not isinstance(texto, str):
        return 'OK', 200
    texto = texto.strip()
    if texto.casefold() in ('⏭️ continuar sin ubicación', '/sinubicacion'):
        descartar_ubicacion(remitente_id)
        enviar_mensaje_telegram(chat_id, 'Continuás sin compartir ubicación. La orden que ya enviaste no cambia.', True)
    elif texto.casefold() in ('/start', '/vincularme', 'vincularme', '📱 vincularme'):
        if remitente_id == MI_TELEGRAM_ID:
            bienvenida = '🇨🇷 Control de sirenas. Primero usá /vincular y /confirmar para autorizar TuyaSmart. /escenas muestra los nombres sin activar nada.'
        else:
            bienvenida = '🇨🇷 Tenés acceso al control de sirenas. Usá los botones para encenderlas o apagarlas.'
        enviar_mensaje_telegram(chat_id, bienvenida, True)
    elif texto in ('/vincular', '/confirmar', '/escenas') and remitente_id != MI_TELEGRAM_ID:
        enviar_mensaje_telegram(chat_id, 'Este comando está reservado a la persona administradora. Podés usar los botones para encender o apagar las sirenas.')
    elif texto == '/vincular':
        iniciar_vinculacion(chat_id)
    elif texto == '/confirmar':
        confirmar_vinculacion(chat_id)
    elif texto == '/escenas':
        listar_escenas(chat_id)
    elif texto.casefold() in ('🟢 encender sirenas', '🔴 apagar sirenas'):
        encender = texto.casefold() == '🟢 encender sirenas'
        enviar_mensaje_telegram(chat_id, '⏳ Procesando la solicitud...')
        exito, detalle = conectar_y_ejecutar_escena(ESCENA_ENCENDER if encender else ESCENA_APAGAR)
        if exito:
            accion = 'encendido' if encender else 'apagado'
            enviar_mensaje_telegram(chat_id, f'✅ Tuya aceptó la orden de {accion}. Verificá el estado de las sirenas.')
            if encender:
                try:
                    encolar_aviso_encendido(remitente_id, update_id)
                except Exception:
                    app.logger.error('Tuya aceptó la orden, pero no se pudo preparar el aviso.')
                    enviar_mensaje_telegram(chat_id, '⚠️ La orden fue aceptada, pero no se pudo preparar el aviso a los demás. No repitás el encendido solo por este fallo.')
                try:
                    solicitar_ubicacion_opcional(chat_id, remitente_id, update_id)
                except Exception:
                    app.logger.error('No se pudo ofrecer la ubicación opcional; la orden ya fue aceptada.')
                    enviar_mensaje_telegram(chat_id, 'No se pudo ofrecer la ubicación opcional. La orden ya fue aceptada; no necesitás repetirla.', True)
            else:
                # Una orden de apagado cierra las solicitudes de ubicación de todos.
                descartar_ubicacion()
        else:
            enviar_mensaje_telegram(chat_id, f'❌ {detalle}')
    else:
        enviar_mensaje_telegram(chat_id, 'Usá los botones para elegir una acción.', True)
    return 'OK', 200


def configurar_menu():
    """Atajos visuales: el usuario toca una opción y Telegram envía el comando."""
    operaciones = [
        ('setMyCommands', {
            'scope': {'type': 'default'},
            'commands': [
                {'command': 'vincularme', 'description': '📱 Vincularme'},
                {'command': 'salir', 'description': '🚪 Salir del bot'},
            ],
        }),
        ('setMyCommands', {
            'scope': {'type': 'chat', 'chat_id': int(MI_TELEGRAM_ID)},
            'commands': [
                {'command': 'start', 'description': 'Panel de administración'},
                {'command': 'vincular', 'description': 'Vincular TuyaSmart'},
                {'command': 'confirmar', 'description': 'Confirmar vinculación de TuyaSmart'},
                {'command': 'escenas', 'description': 'Consultar escenas sin activarlas'},
            ],
        }),
        ('setChatMenuButton', {'menu_button': {'type': 'commands'}}),
    ]
    completo = True
    for metodo, payload in operaciones:
        exito, _ = llamar_telegram(metodo, payload)
        completo = bool(exito) and completo
    return completo


def configurar_webhook():
    if not telegram_configurado():
        raise SystemExit('Revisá TELEGRAM_TOKEN, MI_TELEGRAM_ID y TELEGRAM_WEBHOOK_SECRET (32 a 256 caracteres: letras, números, _ o -). TELEFONOS_AUTORIZADOS debe contener teléfonos internacionales separados por comas.')
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
    if configurar_menu():
        print('Webhook y menú registrados. En Telegram, abrí Menú y tocá Vincularme.')
    else:
        print('Webhook registrado. No se completó la actualización del menú; se puede reintentar configurar-webhook.')


# Reanudar avisos pendientes tras un reinicio si SQLite se conserva. Un solo worker/réplica.
if __name__ != '__main__' and telegram_configurado():
    iniciar_worker_avisos()


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'configurar-webhook':
        configurar_webhook()
    else:
        if telegram_configurado():
            iniciar_worker_avisos()
        app.run(host='0.0.0.0', port=int(valor('PORT', '8080')))
