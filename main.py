import os
import re
import asyncio
import logging
import aiohttp
from typing import Dict, Set, Optional
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
VERSION_MAX = 50000
SCAN_TIMEOUT = 5
MAX_WORKERS = 10

# Almacenamiento
authorized_users: Set[int] = set()
if ADMIN_USER_ID:
    authorized_users.add(ADMIN_USER_ID)

processing_users: Dict[int, Dict] = {}
user_sessions: Dict[int, Dict] = {}
active_scans: Dict[int, asyncio.Task] = {}
user_status_messages: Dict[int, int] = {}  # Almacena ID de mensajes de estado por usuario

# URL base
FIXED_DOWNLOAD_ID = "d794ab9e-2e58-4ac9-97da-237b86d1a6c3"

# Variables globales
telegram_app = None
telethon_client = None
http_session = None

# ========== TECLADOS ==========
def get_cancel_keyboard():
    keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="cancel_operation")]]
    return InlineKeyboardMarkup(keyboard)

def get_digit_selection_keyboard():
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

def get_version_options_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎯 Detección automática", callback_data="auto_detect")],
        [InlineKeyboardButton("🔢 Especificar versión", callback_data="manual_version")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_operation")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== MANEJO DE CALLBACKS ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "cancel_operation":
        await cancel_user_operation(user_id, query)
        return
    
    if data.startswith("digits_"):
        digits = int(data.split("_")[1])
        await handle_digit_selection(user_id, digits, query)
        return
    
    if data == "auto_detect":
        await query.edit_message_text(
            "🎯 *Selecciona el rango de búsqueda:*\n\n"
            "¿Cuántos dígitos tiene la versión?",
            parse_mode='Markdown',
            reply_markup=get_digit_selection_keyboard()
        )
        return
    
    if data == "manual_version":
        await query.edit_message_text(
            "🔢 *Ingresa el número de versión:*\n\n"
            "Ejemplo: `45` para versión 45",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
        return

async def cancel_user_operation(user_id: int, query):
    if user_id in active_scans:
        try:
            active_scans[user_id].cancel()
        except:
            pass
        finally:
            active_scans.pop(user_id, None)
    
    processing_users.pop(user_id, None)
    user_sessions.pop(user_id, None)
    user_status_messages.pop(user_id, None)
    
    await query.edit_message_text("✅ Operación cancelada")

async def handle_digit_selection(user_id: int, digits: int, query):
    if user_id not in user_sessions:
        await query.edit_message_text("⚠️ Sesión expirada. Envía el enlace nuevamente.")
        return
    
    # Calcular rango
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
        await query.edit_message_text("❌ Dígitos no válidos")
        return
    
    package_name = user_sessions[user_id]['package_name']
    
    processing_users[user_id] = {
        'status': 'scanning',
        'package_name': package_name,
        'range': (start, end)
    }
    
    msg = await query.edit_message_text(
        f"🔍 *Escaneando versiones...*\n\n"
        f"📦 *Paquete:* `{package_name}`\n"
        f"📊 *Rango:* {start} - {end}\n"
        f"⏳ *Iniciando...*",
        parse_mode='Markdown',
        reply_markup=get_cancel_keyboard()
    )
    
    # Guardar ID del mensaje de estado
    user_status_messages[user_id] = msg.message_id
    
    # Iniciar escaneo
    task = asyncio.create_task(scan_version_range(user_id, package_name, start, end))
    active_scans[user_id] = task

# ========== ESCANEO OPTIMIZADO ==========
async def check_version_fast(package_name: str, version: int) -> bool:
    """Verificación rápida de versión"""
    url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
    
    try:
        async with http_session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=3)) as response:
            if response.status == 200:
                content_length = response.headers.get('Content-Length', '0')
                return int(content_length) > 1024 * 10  # >10KB
    except Exception as e:
        logger.debug(f"Error versión {version}: {e}")
    return False

async def scan_version_range(user_id: int, package_name: str, start: int, end: int):
    """Escanea rango de versiones"""
    try:
        total = end - start + 1
        found_version = None
        
        # Dividir en bloques para procesamiento concurrente
        block_size = 100
        checked = 0
        
        # Comenzar desde el final (versiones más altas primero)
        for block_start in range(end, start - 1, -block_size):
            block_end = max(start, block_start - block_size + 1)
            versions = list(range(block_start, block_end - 1, -1))
            
            # Crear tareas para este bloque
            tasks = []
            for version in versions:
                if found_version is None:
                    task = asyncio.create_task(check_version_fast(package_name, version))
                    tasks.append((version, task))
            
            # Procesar concurrentemente
            for version, task in tasks:
                if found_version is not None:
                    task.cancel()
                    continue
                    
                try:
                    exists = await asyncio.wait_for(task, timeout=3)
                    if exists:
                        found_version = version
                        logger.info(f"✅ Versión encontrada: {version}")
                        break
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
            
            checked += len(versions)
            progress = (checked / total) * 100
            
            # Actualizar progreso
            try:
                if user_id in user_status_messages:
                    await telegram_app.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=user_status_messages[user_id],
                        text=f"🔍 *Escaneando versiones...*\n\n"
                             f"📦 *Paquete:* `{package_name}`\n"
                             f"📊 *Rango:* {start} - {end}\n"
                             f"✅ *Progreso:* {progress:.1f}%\n"
                             f"🔢 *Verificadas:* {checked}/{total}"
                             f"{f'\n🎯 *Versión encontrada: {found_version}*' if found_version else ''}",
                        parse_mode='Markdown',
                        reply_markup=get_cancel_keyboard() if not found_version else None
                    )
            except Exception as e:
                logger.debug(f"Error actualizando progreso: {e}")
            
            # Si encontramos, salir
            if found_version is not None:
                break
        
        # Resultado final
        if found_version is not None:
            # Pequeña pausa para mostrar resultado
            await asyncio.sleep(1)
            
            # Iniciar descarga
            await start_download(user_id, found_version)
        else:
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=user_status_messages[user_id],
                    text=f"❌ *No se encontró ninguna versión*\n\n"
                         f"📦 *Paquete:* `{package_name}`\n"
                         f"📊 *Rango escaneado:* {start} - {end}",
                    parse_mode='Markdown'
                )
            except:
                pass
            
    except asyncio.CancelledError:
        logger.info(f"Escaneo cancelado para usuario {user_id}")
    except Exception as e:
        logger.error(f"Error en escaneo: {e}")
        try:
            await telegram_app.bot.edit_message_text(
                chat_id=user_id,
                message_id=user_status_messages.get(user_id),
                text=f"❌ *Error en escaneo:*\n`{str(e)[:100]}`",
                parse_mode='Markdown'
            )
        except:
            pass
    finally:
        # Limpiar
        active_scans.pop(user_id, None)
        user_status_messages.pop(user_id, None)

async def start_download(user_id: int, version: int):
    """Inicia descarga después de escaneo"""
    if user_id not in user_sessions:
        return
    
    apk_url = user_sessions[user_id]['apk_url']
    package_name = user_sessions[user_id]['package_name']
    
    try:
        # Enviar mensaje de inicio
        status_msg = await telegram_app.bot.send_message(
            chat_id=user_id,
            text=f"✅ *Versión encontrada: {version}*\n\n"
                 f"⬇️ *Iniciando descarga...*",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
        
        # Guardar ID del mensaje
        user_status_messages[user_id] = status_msg.message_id
        
        # Descargar archivo
        await download_and_send_apk_direct(user_id, package_name, version, status_msg)
        
    except Exception as e:
        logger.error(f"Error iniciando descarga: {e}")
        try:
            await telegram_app.bot.send_message(
                chat_id=user_id,
                text=f"❌ *Error:* `{str(e)[:200]}`",
                parse_mode='Markdown'
            )
        except:
            pass
    finally:
        # Limpiar
        processing_users.pop(user_id, None)
        user_sessions.pop(user_id, None)
        user_status_messages.pop(user_id, None)

# ========== DESCARGA DIRECTA (SIN UPDATE) ==========
async def download_and_send_apk_direct(user_id: int, package_name: str, version: int, status_msg):
    """Descarga y envía APK directamente"""
    try:
        # URL de descarga
        download_url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
        
        # Nombre del archivo
        safe_filename = f"{package_name}-v{version}.apk"
        temp_path = f"temp_{user_id}_{version}.apk"
        
        # 1. Descargar
        await update_status_message(user_id, "📥 *Descargando APK...*")
        
        if not await download_file(download_url, temp_path, user_id):
            await update_status_message(user_id, "❌ *Error en la descarga*")
            return
        
        # Verificar tamaño
        if not os.path.exists(temp_path):
            await update_status_message(user_id, "❌ *Archivo no descargado*")
            return
        
        file_size = os.path.getsize(temp_path)
        if file_size < 1024 * 100:
            await update_status_message(user_id, "❌ *APK inválido (muy pequeño)*")
            os.remove(temp_path)
            return
        
        # 2. Enviar
        await update_status_message(user_id, "📤 *Enviando APK...*")
        
        try:
            with open(temp_path, 'rb') as f:
                await telegram_app.bot.send_document(
                    chat_id=user_id,
                    document=f,
                    filename=safe_filename,
                    caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB",
                    parse_mode='Markdown'
                )
            
            # Eliminar mensaje de estado
            try:
                await telegram_app.bot.delete_message(
                    chat_id=user_id,
                    message_id=user_status_messages.get(user_id)
                )
            except:
                pass
                
        except Exception as e:
            await update_status_message(user_id, f"❌ *Error enviando:* `{str(e)[:100]}`")
        
        # 3. Limpiar
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
    except Exception as e:
        logger.error(f"Error en descarga directa: {e}")
        await update_status_message(user_id, f"❌ *Error:* `{str(e)[:100]}`")

async def update_status_message(user_id: int, text: str):
    """Actualiza mensaje de estado de forma segura"""
    try:
        if user_id in user_status_messages:
            await telegram_app.bot.edit_message_text(
                chat_id=user_id,
                message_id=user_status_messages[user_id],
                text=text,
                parse_mode='Markdown',
                reply_markup=get_cancel_keyboard()
            )
    except Exception as e:
        logger.debug(f"Error actualizando mensaje: {e}")
        # Si falla, enviar nuevo mensaje
        try:
            msg = await telegram_app.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode='Markdown',
                reply_markup=get_cancel_keyboard()
            )
            user_status_messages[user_id] = msg.message_id
        except:
            pass

async def download_file(url: str, filepath: str, user_id: int) -> bool:
    """Descarga archivo con timeout"""
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    total_size = int(response.headers.get('content-length', 0))
                    
                    with open(filepath, 'wb') as f:
                        downloaded = 0
                        
                        async for chunk in response.content.iter_chunked(8192 * 8):
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Actualizar progreso solo para archivos grandes
                            if total_size > 50 * 1024 * 1024 and downloaded % (5 * 1024 * 1024) == 0:
                                percent = (downloaded / total_size) * 100
                                try:
                                    await telegram_app.bot.edit_message_text(
                                        chat_id=user_id,
                                        message_id=user_status_messages.get(user_id),
                                        text=f"📥 *Descargando...*\n"
                                             f"📊 {percent:.1f}%",
                                        parse_mode='Markdown'
                                    )
                                except:
                                    pass
                    
                    return True
                else:
                    logger.error(f"HTTP Error {response.status}")
                    return False
    except Exception as e:
        logger.error(f"Error descargando: {e}")
        return False

# ========== COMANDOS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id in authorized_users:
        welcome_msg = (
            "🤖 *Bot de APKs APKLis*\n\n"
            "📲 *Cómo usar:*\n"
            "1. Envía enlace APKLis.cu\n"
            "2. Selecciona rango de dígitos\n"
            "3. El bot escanea y descarga\n\n"
            "🎯 *Rangos disponibles:*\n"
            "• 1 dígito: 1-9\n"
            "• 2 dígitos: 10-99\n"
            "• 3 dígitos: 100-999\n"
            "• 4 dígitos: 1000-9999\n"
            "• 5 dígitos: 10000-50000\n\n"
            "🛠 *Comandos:*\n"
            "• /cancel - Cancela operación\n"
            "• /status - Estado del bot"
        )
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    else:
        await update.message.reply_text("🔒 No autorizado")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id in active_scans:
        try:
            active_scans[user_id].cancel()
        except:
            pass
        active_scans.pop(user_id, None)
    
    processing_users.pop(user_id, None)
    user_sessions.pop(user_id, None)
    user_status_messages.pop(user_id, None)
    
    await update.message.reply_text("✅ Operaciones canceladas")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        return
    
    active_count = len([t for t in active_scans.values() if not t.done()])
    
    status_msg = (
        f"📊 *Estado del Bot*\n\n"
        f"✅ Operativo\n"
        f"👥 Usuarios: {len(authorized_users)}\n"
        f"🔍 Escaneos: {active_count}\n"
        f"📦 Límite: {VERSION_MAX}"
    )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

# ========== MANEJO DE MENSAJES ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in authorized_users:
        await update.message.reply_text("🔒 No autorizado")
        return
    
    if user_id in processing_users:
        await update.message.reply_text("⏳ Operación en curso. Usa /cancel")
        return
    
    text = update.message.text.strip()
    
    # Buscar enlace
    apklis_pattern = r'https?://(?:www\.)?apklis\.cu/application/([a-zA-Z0-9._-]+)'
    match = re.search(apklis_pattern, text)
    
    if match:
        package_name = match.group(1)
        
        user_sessions[user_id] = {
            'apk_url': match.group(0),
            'package_name': package_name
        }
        
        await update.message.reply_text(
            f"✅ *Enlace detectado:*\n`{package_name}`\n\n"
            f"🎯 *Selecciona opción:*",
            parse_mode='Markdown',
            reply_markup=get_version_options_keyboard()
        )
        
    elif user_id in user_sessions and text.isdigit():
        version = int(text)
        
        if version < 1 or version > VERSION_MAX:
            await update.message.reply_text(
                f"❌ Rango: 1-{VERSION_MAX}",
                reply_markup=get_cancel_keyboard()
            )
            return
        
        package_name = user_sessions[user_id]['package_name']
        
        # Verificar versión
        await update.message.reply_text(f"🔍 Verificando versión {version}...")
        
        if await check_version_fast(package_name, version):
            # Iniciar descarga manual
            status_msg = await update.message.reply_text(
                f"✅ *Versión {version} encontrada*\n"
                f"⬇️ *Descargando...*",
                parse_mode='Markdown',
                reply_markup=get_cancel_keyboard()
            )
            
            user_status_messages[user_id] = status_msg.message_id
            processing_users[user_id] = {'status': 'downloading'}
            
            await download_and_send_apk_direct(
                user_id, 
                package_name, 
                version, 
                status_msg
            )
        else:
            await update.message.reply_text(f"❌ Versión {version} no existe")
            user_sessions.pop(user_id, None)
            
    else:
        await update.message.reply_text(
            "📝 Envía enlace APKLis.cu",
            parse_mode='Markdown'
        )

# ========== INICIALIZACIÓN ==========
async def init_telethon():
    global telethon_client
    if API_ID and API_HASH:
        try:
            telethon_client = TelegramClient('session', int(API_ID), API_HASH)
            await telethon_client.start()
            logger.info("✅ Telethon iniciado")
        except Exception as e:
            logger.error(f"❌ Telethon: {e}")
            telethon_client = None

async def init_http_session():
    global http_session
    connector = aiohttp.TCPConnector(limit=MAX_WORKERS)
    timeout = aiohttp.ClientTimeout(total=SCAN_TIMEOUT)
    http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    logger.info("✅ HTTP Session iniciada")

async def close_http_session():
    global http_session
    if http_session:
        await http_session.close()
        logger.info("✅ HTTP Session cerrada")

# ========== SERVIDOR WEB ==========
async def health_check(request):
    return web.json_response({
        "status": "healthy",
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
    
    logger.info("🚀 Iniciando bot...")
    
    await init_http_session()
    await init_telethon()
    
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("✅ Bot iniciado")
    
    web_runner = await start_web_server()
    
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("👋 Apagando...")
    finally:
        # Limpiar
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
        logger.error(f"❌ Error: {e}")
    finally:
        loop.close()

if __name__ == '__main__':
    run()
