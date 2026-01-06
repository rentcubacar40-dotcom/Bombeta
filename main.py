import os
import re
import asyncio
import logging
import aiohttp
from typing import Dict, Set, Optional, Tuple
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from aiohttp import web
from telethon import TelegramClient
import hashlib
import time

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Variables de entorno
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 0))
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
PORT = int(os.getenv('PORT', 10000))

# Validar variables críticas
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no configurado")
if not API_ID or not API_HASH:
    logger.warning("API_ID o API_HASH no configurados. Descargas limitadas a 50MB")

# Configuración
VERSION_MAX = 50000  # Límite máximo
SCAN_TIMEOUT = 5  # Segundos por intento
MAX_WORKERS = 10  # Máximo de verificaciones concurrentes

# Almacenamiento en memoria
authorized_users: Set[int] = set()
if ADMIN_USER_ID:
    authorized_users.add(ADMIN_USER_ID)

processing_users: Dict[int, Dict] = {}  # Usuarios procesando con su estado
user_sessions: Dict[int, Dict] = {}  # Almacena URLs pendientes por usuario
active_scans: Dict[int, asyncio.Task] = {}  # Tareas de escaneo activas

# URL base fija
FIXED_DOWNLOAD_ID = "d794ab9e-2e58-4ac9-97da-237b86d1a6c3"

# Variable global para la aplicación
telegram_app = None
telethon_client = None
http_session = None

# ========== TECLADOS INLINE ==========
def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Teclado para cancelar operaciones"""
    keyboard = [
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_operation")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_digit_selection_keyboard() -> InlineKeyboardMarkup:
    """Teclado para seleccionar dígitos"""
    keyboard = [
        [
            InlineKeyboardButton("1 dígito (1-9)", callback_data="digits_1"),
            InlineKeyboardButton("2 dígitos (10-99)", callback_data="digits_2")
        ],
        [
            InlineKeyboardButton("3 dígitos (100-999)", callback_data="digits_3"),
            InlineKeyboardButton("4 dígitos (1000-9999)", callback_data="digits_4")
        ],
        [
            InlineKeyboardButton("5 dígitos (10000-50000)", callback_data="digits_5"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancel_operation")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_version_options_keyboard() -> InlineKeyboardMarkup:
    """Teclado para opciones de versión"""
    keyboard = [
        [InlineKeyboardButton("🎯 Detección automática", callback_data="auto_detect")],
        [InlineKeyboardButton("🔢 Especificar versión", callback_data="manual_version")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_operation")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== MANEJO DE CALLBACKS ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja callbacks de botones inline"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    # Cancelar operación
    if data == "cancel_operation":
        await cancel_user_operation(user_id, query)
        return
    
    # Selección de dígitos
    if data.startswith("digits_"):
        digits = int(data.split("_")[1])
        await handle_digit_selection(user_id, digits, query, context)
        return
    
    # Opciones de versión
    if data == "auto_detect":
        await handle_auto_detect(user_id, query, context)
        return
    
    if data == "manual_version":
        await query.edit_message_text(
            "🔢 *Ingresa el número de versión:*\n\n"
            "Ejemplo: `45` para versión 45",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
        return
    
    await query.edit_message_text("⚠️ Opción no reconocida")

async def cancel_user_operation(user_id: int, query):
    """Cancela todas las operaciones del usuario"""
    # Cancelar escaneo activo
    if user_id in active_scans:
        try:
            active_scans[user_id].cancel()
            logger.info(f"Escaneo cancelado para usuario {user_id}")
        except:
            pass
        finally:
            active_scans.pop(user_id, None)
    
    # Limpiar estados
    processing_users.pop(user_id, None)
    user_sessions.pop(user_id, None)
    
    await query.edit_message_text("✅ Operación cancelada")

async def handle_digit_selection(user_id: int, digits: int, query, context):
    """Maneja la selección de dígitos"""
    if user_id not in user_sessions:
        await query.edit_message_text("⚠️ Sesión expirada. Envía el enlace nuevamente.")
        return
    
    # Calcular rango basado en dígitos
    if digits == 1:
        start, end = 1, 9
    elif digits == 2:
        start, end = 10, 99
    elif digits == 3:
        start, end = 100, 999
    elif digits == 4:
        start, end = 1000, 9999
    elif digits == 5:
        start, end = 10000, VERSION_MAX
    else:
        await query.edit_message_text("❌ Número de dígitos no válido")
        return
    
    package_name = user_sessions[user_id].get('package_name')
    
    # Iniciar escaneo
    processing_users[user_id] = {
        'status': 'scanning',
        'package_name': package_name,
        'range': (start, end),
        'current': start,
        'found': None
    }
    
    await query.edit_message_text(
        f"🔍 *Escaneando versiones...*\n\n"
        f"📊 *Rango:* {start} - {end}\n"
        f"📦 *Paquete:* `{package_name}`\n\n"
        f"⏳ *Escaneando... 0%*",
        parse_mode='Markdown',
        reply_markup=get_cancel_keyboard()
    )
    
    # Iniciar tarea de escaneo
    task = asyncio.create_task(
        scan_version_range(user_id, package_name, start, end, query.message.message_id)
    )
    active_scans[user_id] = task

async def handle_auto_detect(user_id: int, query, context):
    """Inicia detección automática con todos los dígitos"""
    if user_id not in user_sessions:
        await query.edit_message_text("⚠️ Sesión expirada. Envía el enlace nuevamente.")
        return
    
    package_name = user_sessions[user_id].get('package_name')
    
    await query.edit_message_text(
        "🎯 *Selecciona el rango de búsqueda:*\n\n"
        "¿Cuántos dígitos tiene la versión?",
        parse_mode='Markdown',
        reply_markup=get_digit_selection_keyboard()
    )

# ========== ESCANEO DE VERSIONES ==========
async def check_version_fast(package_name: str, version: int) -> bool:
    """Verificación rápida de versión con timeout corto"""
    url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
    
    try:
        async with http_session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=3)) as response:
            if response.status == 200:
                content_length = response.headers.get('Content-Length', '0')
                return int(content_length) > 1024 * 10  # Al menos 10KB
    except:
        pass
    return False

async def scan_version_range(user_id: int, package_name: str, start: int, end: int, message_id: int):
    """Escanea un rango de versiones de forma concurrente"""
    try:
        total_versions = end - start + 1
        versions_to_check = list(range(start, end + 1))
        found_version = None
        
        # Dividir en lotes para procesamiento concurrente
        batch_size = 50
        completed = 0
        
        for batch_start in range(0, len(versions_to_check), batch_size):
            batch = versions_to_check[batch_start:batch_start + batch_size]
            
            # Verificar versión más alta primero (más probable)
            batch.reverse()
            
            # Crear tareas para este lote
            tasks = []
            for version in batch:
                if found_version is None:  # Solo continuar si no hemos encontrado
                    task = asyncio.create_task(check_version_fast(package_name, version))
                    tasks.append((version, task))
            
            # Ejecutar concurrentemente
            for version, task in tasks:
                if found_version is not None:
                    task.cancel()
                    continue
                    
                try:
                    exists = await asyncio.wait_for(task, timeout=3)
                    if exists:
                        found_version = version
                        logger.info(f"✅ Versión encontrada: {version}")
                        
                        # Cancelar todas las demás tareas
                        for v, t in tasks:
                            if v != version:
                                t.cancel()
                        break
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Error checking version {version}: {e}")
            
            completed += len(batch)
            progress = (completed / total_versions) * 100
            
            # Actualizar progreso
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=f"🔍 *Escaneando versiones...*\n\n"
                         f"📊 *Rango:* {start} - {end}\n"
                         f"📦 *Paquete:* `{package_name}`\n"
                         f"✅ *Completado:* {progress:.1f}%\n"
                         f"🔢 *Verificadas:* {completed}/{total_versions}\n"
                         f"{'🎯 *Versión encontrada!*' if found_version else ''}",
                    parse_mode='Markdown',
                    reply_markup=get_cancel_keyboard()
                )
                
                # Si encontramos versión, detener
                if found_version is not None:
                    processing_users[user_id]['found'] = found_version
                    processing_users[user_id]['status'] = 'found'
                    
                    # Pequeña pausa para mostrar resultado
                    await asyncio.sleep(1)
                    
                    # Iniciar descarga automáticamente
                    await start_download_after_scan(user_id, found_version, message_id)
                    return
                    
            except Exception as e:
                logger.error(f"Error updating progress: {e}")
        
        # Si llegamos aquí y no encontramos
        if found_version is None:
            await telegram_app.bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=f"❌ *No se encontró ninguna versión*\n\n"
                     f"📦 *Paquete:* `{package_name}`\n"
                     f"📊 *Rango escaneado:* {start} - {end}\n\n"
                     f"⚠️ *Posibles causas:*\n"
                     f"• El paquete no existe\n"
                     f"• Las versiones están fuera del rango\n"
                     f"• Intenta con otro rango de dígitos",
                parse_mode='Markdown'
            )
            processing_users.pop(user_id, None)
            user_sessions.pop(user_id, None)
            
    except asyncio.CancelledError:
        logger.info(f"Escaneo cancelado para usuario {user_id}")
    except Exception as e:
        logger.error(f"Error en escaneo: {e}")
        try:
            await telegram_app.bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=f"❌ *Error en el escaneo*\n\n`{str(e)[:100]}`",
                parse_mode='Markdown'
            )
        except:
            pass
    finally:
        active_scans.pop(user_id, None)

async def start_download_after_scan(user_id: int, version: int, message_id: int):
    """Inicia descarga después de encontrar versión"""
    if user_id not in user_sessions:
        return
    
    apk_url = user_sessions[user_id]['apk_url']
    
    try:
        # Actualizar mensaje
        await telegram_app.bot.edit_message_text(
            chat_id=user_id,
            message_id=message_id,
            text=f"✅ *Versión encontrada: {version}*\n\n"
                 f"⬇️ *Iniciando descarga...*",
            parse_mode='Markdown'
        )
        
        # Crear contexto artificial para la descarga
        class FakeUpdate:
            def __init__(self, user_id):
                self.effective_user = type('obj', (object,), {'id': user_id})()
                self.effective_chat = type('obj', (object,), {'id': user_id})()
                self.message = type('obj', (object,), {
                    'message_id': message_id,
                    'reply_text': self.reply_text
                })()
            
            async def reply_text(self, text, **kwargs):
                await telegram_app.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    **kwargs
                )
        
        fake_update = FakeUpdate(user_id)
        
        # Ejecutar descarga
        await download_and_send_apk(fake_update, telegram_app, apk_url, str(version))
        
    except Exception as e:
        logger.error(f"Error iniciando descarga: {e}")
        try:
            await telegram_app.bot.send_message(
                chat_id=user_id,
                text=f"❌ *Error en la descarga:*\n`{str(e)[:200]}`",
                parse_mode='Markdown'
            )
        except:
            pass
    finally:
        # Limpiar
        processing_users.pop(user_id, None)
        user_sessions.pop(user_id, None)

# ========== INICIALIZACIÓN ==========
async def init_telethon():
    """Inicializa cliente Telethon"""
    global telethon_client
    
    if API_ID and API_HASH:
        try:
            telethon_client = TelegramClient(
                'apk_bot_session',
                int(API_ID),
                API_HASH
            )
            await telethon_client.start()
            logger.info("✅ Cliente Telethon iniciado")
        except Exception as e:
            logger.error(f"❌ Error iniciando Telethon: {e}")
            telethon_client = None

async def init_http_session():
    """Inicializa sesión HTTP"""
    global http_session
    connector = aiohttp.TCPConnector(limit=MAX_WORKERS, force_close=True)
    timeout = aiohttp.ClientTimeout(total=SCAN_TIMEOUT, connect=3, sock_read=3)
    http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    logger.info("✅ Sesión HTTP inicializada")

async def close_http_session():
    """Cierra sesión HTTP"""
    global http_session
    if http_session:
        await http_session.close()
        logger.info("✅ Sesión HTTP cerrada")

# ========== COMANDOS DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    user_id = update.effective_user.id
    
    if user_id in authorized_users:
        welcome_msg = (
            "🤖 *Bot de APKs APKLis 2026*\n\n"
            "📲 *Cómo usar:*\n"
            "1. Envía enlace APKLis.cu\n"
            "2. Selecciona rango de dígitos\n"
            "3. El bot escanea y descarga automáticamente\n\n"
            "⚡ *Escaneo por dígitos:*\n"
            "• 1 dígito: 1-9\n"
            "• 2 dígitos: 10-99\n"
            "• 3 dígitos: 100-999\n"
            "• 4 dígitos: 1000-9999\n"
            "• 5 dígitos: 10000-50000\n\n"
            "🛠 *Comandos:*\n"
            "• /cancel - Cancela operación actual\n"
            "• /status - Estado del bot"
        )
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "🔒 *Acceso restringido*\n\n"
            "Contacta al administrador para acceder.",
            parse_mode='Markdown'
        )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /cancel"""
    user_id = update.effective_user.id
    
    # Cancelar escaneo activo
    if user_id in active_scans:
        try:
            active_scans[user_id].cancel()
            await update.message.reply_text("✅ Escaneo cancelado")
        except:
            pass
        finally:
            active_scans.pop(user_id, None)
    
    # Limpiar estados
    processing_users.pop(user_id, None)
    user_sessions.pop(user_id, None)
    
    await update.message.reply_text("✅ Todas las operaciones canceladas")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status"""
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        return
    
    active_scans_count = len([t for t in active_scans.values() if not t.done()])
    
    status_msg = (
        f"📊 *Estado del Bot*\n\n"
        f"✅ Bot: Operativo\n"
        f"👥 Usuarios: {len(authorized_users)}\n"
        f"🔍 Escaneos activos: {active_scans_count}\n"
        f"📦 Descargas: {len([u for u in processing_users.values() if u.get('status') == 'downloading'])}\n"
        f"💾 Telethon: {'✅' if telethon_client else '❌'}\n"
        f"🎯 Límite versión: {VERSION_MAX}"
    )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

# ========== MANEJO DE MENSAJES ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes de texto"""
    user_id = update.effective_user.id
    
    # Verificar autorización
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 No autorizado")
        return
    
    # Verificar si ya está procesando
    if user_id in processing_users:
        await update.message.reply_text(
            "⏳ Ya tienes una operación en curso.\n"
            "Usa /cancel para detenerla.",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    text = update.message.text.strip()
    
    # Buscar enlace APKLis
    apklis_pattern = r'https?://(?:www\.)?apklis\.cu/application/([a-zA-Z0-9._-]+)'
    match = re.search(apklis_pattern, text)
    
    if match:
        package_name = match.group(1)
        
        # Guardar URL en sesión
        user_sessions[user_id] = {
            'apk_url': match.group(0),
            'package_name': package_name
        }
        
        # Mostrar opciones
        await update.message.reply_text(
            f"✅ *Enlace detectado:*\n`{package_name}`\n\n"
            f"🎯 *Selecciona una opción:*",
            parse_mode='Markdown',
            reply_markup=get_version_options_keyboard()
        )
        
    elif user_id in user_sessions and 'apk_url' in user_sessions[user_id]:
        # Si el usuario ingresa un número manualmente
        if text.isdigit():
            version = int(text)
            
            # Validar rango
            if version < 1 or version > VERSION_MAX:
                await update.message.reply_text(
                    f"❌ *Versión fuera de rango*\n\n"
                    f"Rango permitido: 1 - {VERSION_MAX}\n"
                    f"Ingresa una versión válida:",
                    parse_mode='Markdown',
                    reply_markup=get_cancel_keyboard()
                )
                return
            
            apk_url = user_sessions[user_id]['apk_url']
            
            # Marcar como procesando
            processing_users[user_id] = {'status': 'downloading'}
            
            try:
                # Verificar si existe primero
                await update.message.reply_text(
                    f"🔍 *Verificando versión {version}...*",
                    parse_mode='Markdown',
                    reply_markup=get_cancel_keyboard()
                )
                
                if await check_version_fast(user_sessions[user_id]['package_name'], version):
                    # Iniciar descarga
                    await download_and_send_apk(update, context, apk_url, str(version))
                else:
                    await update.message.reply_text(
                        f"❌ *Versión {version} no encontrada*\n\n"
                        f"La versión especificada no existe.\n"
                        f"Prueba con detección automática.",
                        parse_mode='Markdown'
                    )
                    
            except Exception as e:
                logger.error(f"Error en descarga manual: {e}")
                await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
            finally:
                # Limpiar
                processing_users.pop(user_id, None)
                user_sessions.pop(user_id, None)
        else:
            await update.message.reply_text(
                "📝 *Envía:*\n"
                "1. Un enlace de APKLis.cu\n"
                "O después del enlace:\n"
                "2. Un número de versión",
                parse_mode='Markdown'
            )
    else:
        await update.message.reply_text(
            "📝 *Envía un enlace de APKLis.cu*\n\n"
            "Ejemplo: `https://apklis.cu/application/com.example.app`",
            parse_mode='Markdown'
        )

# ========== DESCARGA RÁPIDA ==========
async def download_with_progress(url: str, filepath: str, update: Update, status_msg):
    """Descarga con progreso optimizado"""
    try:
        timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=30)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    total_size = int(response.headers.get('content-length', 0))
                    
                    with open(filepath, 'wb') as f:
                        downloaded = 0
                        chunk_size = 1024 * 64  # 64KB chunks
                        last_update = 0
                        
                        async for chunk in response.content.iter_chunked(chunk_size):
                            if downloaded == 0:
                                await status_msg.edit_text("📥 *Descargando...*", parse_mode='Markdown')
                            
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Actualizar progreso solo para archivos grandes y no tan frecuente
                            if total_size > 50 * 1024 * 1024:  # >50MB
                                percent = (downloaded / total_size) * 100
                                current_time = time.time()
                                
                                # Actualizar máximo cada 10% o 5 segundos
                                if percent - last_update >= 10 or current_time - last_update >= 5:
                                    try:
                                        await status_msg.edit_text(
                                            f"📥 *Descargando...*\n"
                                            f"📊 {percent:.0f}% ({downloaded/1024/1024:.1f}MB/{total_size/1024/1024:.1f}MB)",
                                            parse_mode='Markdown'
                                        )
                                        last_update = percent
                                    except:
                                        pass
                    
                    return True
                else:
                    logger.error(f"HTTP Error {response.status}")
                    return False
    except Exception as e:
        logger.error(f"Error en descarga: {e}")
        return False

async def download_and_send_apk(update: Update, context, apk_url: str, version: str):
    """Descarga y envía el APK optimizado"""
    user_id = update.effective_user.id
    
    if user_id in processing_users and processing_users[user_id].get('status') == 'cancelled':
        return
    
    status_msg = await update.message.reply_text(
        f"🔄 *Preparando versión {version}...*",
        parse_mode='Markdown',
        reply_markup=get_cancel_keyboard()
    )
    
    try:
        # Extraer información
        package_name = apk_url.split('/')[-1]
        
        # Generar URL de descarga
        download_url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
        
        # Nombre del archivo
        safe_filename = f"{package_name}-v{version}.apk"
        temp_path = f"temp_{hashlib.md5(safe_filename.encode()).hexdigest()[:8]}.apk"
        
        # Descargar
        await status_msg.edit_text("📥 *Descargando APK...*", parse_mode='Markdown')
        
        download_success = await download_with_progress(download_url, temp_path, update, status_msg)
        
        if not download_success or not os.path.exists(temp_path):
            await status_msg.edit_text("❌ *Error en la descarga*", parse_mode='Markdown')
            return
        
        # Verificar tamaño
        file_size = os.path.getsize(temp_path)
        
        if file_size < 1024 * 100:
            await status_msg.edit_text("❌ *APK inválido*", parse_mode='Markdown')
            os.remove(temp_path)
            return
        
        # Enviar
        await status_msg.edit_text("📤 *Enviando APK...*", parse_mode='Markdown')
        
        if file_size > 50 * 1024 * 1024 and telethon_client:
            try:
                await telethon_client.send_file(
                    await telethon_client.get_input_entity(user_id),
                    temp_path,
                    caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB",
                    force_document=True
                )
                await status_msg.delete()
            except Exception as e:
                await status_msg.edit_text(f"❌ Error enviando: {str(e)[:100]}", parse_mode='Markdown')
        else:
            try:
                with open(temp_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=f,
                        filename=safe_filename,
                        caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB",
                        parse_mode='Markdown'
                    )
                await status_msg.delete()
            except Exception as e:
                await status_msg.edit_text(f"❌ Error enviando: {str(e)[:100]}", parse_mode='Markdown')
                
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text(f"❌ *Error:* `{str(e)[:100]}`", parse_mode='Markdown')
    finally:
        # Limpiar
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        processing_users.pop(user_id, None)
        user_sessions.pop(user_id, None)

# ========== SERVIDOR WEB ==========
async def health_check(request):
    return web.json_response({
        "status": "healthy",
        "version_max": VERSION_MAX,
        "active_scans": len(active_scans),
        "active_downloads": len([u for u in processing_users.values() if u.get('status') == 'downloading'])
    })

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"🌐 Servidor web en puerto {PORT}")
    return runner

# ========== MAIN ==========
async def main():
    global telegram_app
    
    logger.info("🚀 Iniciando bot con escaneo por dígitos...")
    
    # Inicializar
    await init_http_session()
    await init_telethon()
    
    # Crear app de Telegram
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Iniciar bot
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("✅ Bot iniciado")
    
    # Servidor web
    web_runner = await start_web_server()
    
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("👋 Apagando...")
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        
        if telethon_client:
            await telethon_client.disconnect()
        
        await close_http_session()
        await web_runner.cleanup()

def run():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Detenido por usuario")
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}")
    finally:
        loop.close()

if __name__ == '__main__':
    run()
