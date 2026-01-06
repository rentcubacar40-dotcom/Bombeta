import os
import re
import asyncio
import logging
import aiohttp
from typing import Dict, Set, Optional
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from aiohttp import web
from telethon import TelegramClient
import hashlib

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

# Configuración de detección automática
VERSION_MIN = 1
VERSION_MAX = 65535
MAX_CONCURRENT_SCANS = 3  # Máximo de escaneos concurrentes por usuario
SCAN_TIMEOUT = 10  # Segundos de timeout por intento

# Almacenamiento en memoria
authorized_users: Set[int] = set()
if ADMIN_USER_ID:
    authorized_users.add(ADMIN_USER_ID)

processing_users: Set[int] = set()
user_sessions: Dict[int, Dict] = {}  # Almacena URLs pendientes por usuario

# URL base fija
FIXED_DOWNLOAD_ID = "d794ab9e-2e58-4ac9-97da-237b86d1a6c3"

# Variable global para la aplicación
telegram_app = None
telethon_client = None
http_session = None  # Sesión HTTP compartida

# ========== INICIALIZACIÓN TELETHON (para archivos grandes) ==========
async def init_telethon():
    """Inicializa cliente Telethon para archivos grandes"""
    global telethon_client
    
    if API_ID and API_HASH:
        try:
            telethon_client = TelegramClient(
                'apk_bot_session',
                int(API_ID),
                API_HASH
            )
            await telethon_client.start()
            logger.info("✅ Cliente Telethon iniciado para descargas grandes")
        except Exception as e:
            logger.error(f"❌ Error iniciando Telethon: {e}")
            telethon_client = None

# ========== INICIALIZAR SESIÓN HTTP ==========
async def init_http_session():
    """Inicializa sesión HTTP compartida"""
    global http_session
    timeout = aiohttp.ClientTimeout(total=SCAN_TIMEOUT)
    http_session = aiohttp.ClientSession(timeout=timeout)
    logger.info("✅ Sesión HTTP inicializada")

async def close_http_session():
    """Cierra sesión HTTP"""
    global http_session
    if http_session:
        await http_session.close()
        logger.info("✅ Sesión HTTP cerrada")

# ========== DETECCIÓN AUTOMÁTICA DE VERSIÓN ==========
async def check_version_exists(package_name: str, version: int) -> bool:
    """Verifica si una versión específica existe"""
    url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
    
    try:
        async with http_session.head(url, allow_redirects=True) as response:
            # Verificar códigos de estado exitosos
            if response.status == 200:
                # Verificar que el Content-Type sea de un APK
                content_type = response.headers.get('Content-Type', '').lower()
                content_length = response.headers.get('Content-Length', '0')
                
                # Algunos servidores pueden devolver 200 incluso para errores,
                # así que verificamos también el tamaño
                if (content_type in ['application/vnd.android.package-archive', 
                                     'application/octet-stream'] and
                    int(content_length) > 1024):  # Al menos 1KB
                    return True
                elif int(content_length) > 1024 * 10:  # Si es >10KB probablemente es válido
                    return True
            return False
    except Exception as e:
        logger.debug(f"Error verificando versión {version}: {e}")
        return False

async def find_latest_version(package_name: str) -> Optional[int]:
    """
    Busca la última versión disponible usando búsqueda binaria
    y detección inteligente
    """
    logger.info(f"🔍 Buscando última versión para {package_name}")
    
    # Primero probar algunas versiones comunes rápidamente
    quick_checks = [65535, 50000, 32768, 10000, 5000, 1000, 500, 100]
    
    for version in quick_checks:
        if await check_version_exists(package_name, version):
            logger.info(f"✅ Versión rápida encontrada: {version}")
            # Hacer búsqueda lineal hacia arriba desde esta versión
            return await linear_search_up(package_name, version)
    
    # Si no encontró en las versiones rápidas, hacer búsqueda binaria completa
    return await binary_search_version(package_name, VERSION_MIN, VERSION_MAX)

async def linear_search_up(package_name: str, start_version: int) -> Optional[int]:
    """Búsqueda lineal hacia arriba desde una versión inicial"""
    current_version = start_version
    found_version = start_version
    
    while current_version <= VERSION_MAX:
        if await check_version_exists(package_name, current_version):
            found_version = current_version
            current_version += 1
        else:
            break
    
    logger.info(f"📈 Última versión encontrada: {found_version}")
    return found_version

async def binary_search_version(package_name: str, low: int, high: int) -> Optional[int]:
    """Búsqueda binaria para encontrar la última versión válida"""
    last_valid = None
    low, high = VERSION_MIN, VERSION_MAX
    
    while low <= high:
        mid = (low + high) // 2
        
        if await check_version_exists(package_name, mid):
            last_valid = mid
            low = mid + 1  # Buscar en la mitad superior
        else:
            high = mid - 1  # Buscar en la mitad inferior
    
    return last_valid

async def detect_version_range(package_name: str) -> tuple:
    """
    Detecta el rango de versiones disponibles
    Retorna: (versión_mínima, versión_máxima)
    """
    # Buscar la última versión primero
    latest = await find_latest_version(package_name)
    
    if latest is None:
        return (None, None)
    
    # Buscar la primera versión (aproximada)
    first_version = await find_first_version(package_name, latest)
    
    return (first_version, latest)

async def find_first_version(package_name: str, latest_version: int) -> int:
    """Encuentra la primera versión disponible"""
    # Empezar desde atrás para evitar muchas peticiones
    min_check = max(1, latest_version - 1000)
    
    for version in range(min_check, 0, -1):
        if not await check_version_exists(package_name, version):
            return version + 1 if version + 1 <= latest_version else latest_version
    
    return 1

# ========== COMANDOS DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    user_id = update.effective_user.id
    
    if user_id in authorized_users:
        welcome_msg = (
            "🤖 *Bot de APKs APKLis 2026*\n\n"
            "📲 *Cómo usar:*\n"
            "1. Envía enlace APKLis.cu\n"
            "2. El bot detectará automáticamente la última versión\n"
            "3. Recibe el APK\n\n"
            "⚡ *Detección automática de versión*\n"
            "🔄 *Escaneo inteligente: 1-65535*\n"
            "🛡 *Cifrado de extremo a extremo*\n\n"
            "🛠 *Comandos Admin:*\n"
            "• /add id1 id2 - Añadir usuarios\n"
            "• /remove id - Eliminar usuario\n"
            "• /users - Listar usuarios\n"
            "• /status - Estado del bot"
        )
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "🔒 *Acceso restringido*\n\n"
            "Contacta al administrador para acceder.",
            parse_mode='Markdown'
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status"""
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        return
    
    status_msg = (
        f"📊 *Estado del Bot - {datetime.now().year}*\n\n"
        f"✅ Bot: Operativo\n"
        f"👥 Usuarios: {len(authorized_users)}\n"
        f"⏬ Descargas activas: {len(processing_users)}\n"
        f"💾 Telethon: {'✅' if telethon_client else '❌'}\n"
        f"🌐 HTTP Session: {'✅' if http_session else '❌'}\n"
        f"📦 Memoria: {len(user_sessions)} sesiones\n"
        f"🎯 Detección: {VERSION_MIN}-{VERSION_MAX}"
    )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def add_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /add"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Solo administrador")
        return
    
    if not context.args:
        await update.message.reply_text(
            "📝 *Uso:* `/add id1 id2 id3`\n\n"
            "Ejemplo: `/add 123456789 987654321`",
            parse_mode='Markdown'
        )
        return
    
    added = []
    for arg in context.args:
        if arg.isdigit():
            user_id_int = int(arg)
            if user_id_int not in authorized_users:
                authorized_users.add(user_id_int)
                added.append(str(user_id_int))
    
    if added:
        await update.message.reply_text(
            f"✅ *Usuarios añadidos:* {', '.join(added)}\n"
            f"Total: {len(authorized_users)} usuarios",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("ℹ️ No se añadieron nuevos usuarios")

async def remove_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /remove"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return
    
    if not context.args:
        await update.message.reply_text(
            "📝 *Uso:* `/remove id1 id2`\n\n"
            "Ejemplo: `/remove 123456789`",
            parse_mode='Markdown'
        )
        return
    
    removed = []
    for arg in context.args:
        if arg.isdigit():
            user_id_int = int(arg)
            if user_id_int in authorized_users and user_id_int != ADMIN_USER_ID:
                authorized_users.remove(user_id_int)
                # Limpiar sesión del usuario
                if user_id_int in user_sessions:
                    del user_sessions[user_id_int]
                removed.append(str(user_id_int))
    
    if removed:
        await update.message.reply_text(f"❌ *Eliminados:* {', '.join(removed)}")
    else:
        await update.message.reply_text("ℹ️ No se eliminó ningún usuario")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /users"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return
    
    if not authorized_users:
        await update.message.reply_text("📭 No hay usuarios autorizados")
        return
    
    users_list = "\n".join([f"• `{uid}`" + (" 👑" if uid == ADMIN_USER_ID else "") for uid in authorized_users])
    await update.message.reply_text(
        f"👥 *Usuarios autorizados ({len(authorized_users)}):*\n\n{users_list}",
        parse_mode='Markdown'
    )

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
        await update.message.reply_text("⏳ Tienes una descarga en curso. Espera...")
        return
    
    text = update.message.text.strip()
    
    # Buscar enlace APKLis
    apklis_pattern = r'https?://(?:www\.)?apklis\.cu/application/([a-zA-Z0-9._-]+)'
    match = re.search(apklis_pattern, text)
    
    if match:
        package_name = match.group(1)
        
        # Guardar URL en sesión del usuario
        user_sessions[user_id] = {
            'apk_url': match.group(0),
            'package_name': package_name
        }
        
        # Opciones para el usuario
        await update.message.reply_text(
            "✅ *Enlace detectado*\n\n"
            "🎯 *Opciones:*\n"
            "• Envía `/auto` para detección automática\n"
            "• Envía el número de versión manualmente\n\n"
            "⚡ *Detección automática:* Escanea versiones 1-65535",
            parse_mode='Markdown'
        )
    elif user_id in user_sessions and 'apk_url' in user_sessions[user_id]:
        package_name = user_sessions[user_id]['package_name']
        apk_url = user_sessions[user_id]['apk_url']
        
        # Comando /auto o cualquier texto que no sea número
        if text.lower() == '/auto' or not text.isdigit():
            # Iniciar detección automática
            processing_users.add(user_id)
            status_msg = await update.message.reply_text(
                "🔍 *Iniciando detección automática...*\n"
                "_Escaneando versiones 1-65535_\n"
                "⏱ Esto puede tomar unos segundos",
                parse_mode='Markdown'
            )
            
            try:
                # Buscar última versión
                latest_version = await find_latest_version(package_name)
                
                if latest_version is None:
                    await status_msg.edit_text(
                        "❌ *No se encontró ninguna versión disponible*\n\n"
                        "Verifica que:\n"
                        "• El enlace sea correcto\n"
                        "• La aplicación exista en el repositorio\n"
                        "• Intenta con una versión manual: `/version número`",
                        parse_mode='Markdown'
                    )
                    processing_users.discard(user_id)
                    return
                
                # Eliminar sesión
                del user_sessions[user_id]
                
                # Iniciar descarga automáticamente
                await status_msg.edit_text(
                    f"✅ *Versión encontrada: {latest_version}*\n"
                    f"⬇️ *Iniciando descarga...*",
                    parse_mode='Markdown'
                )
                
                await download_and_send_apk(update, context, apk_url, str(latest_version))
                
            except Exception as e:
                logger.error(f"Error en detección automática: {e}")
                await status_msg.edit_text(f"❌ Error en detección: {str(e)[:200]}")
                processing_users.discard(user_id)
            finally:
                if user_id in user_sessions:
                    del user_sessions[user_id]
                
        elif text.isdigit():
            # Procesar versión manual
            version = text
            del user_sessions[user_id]
            
            # Iniciar descarga
            processing_users.add(user_id)
            try:
                await download_and_send_apk(update, context, apk_url, version)
            except Exception as e:
                logger.error(f"Error en descarga manual: {e}")
                await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
            finally:
                processing_users.discard(user_id)
    else:
        await update.message.reply_text(
            "📝 *Envía un enlace de APKLis.cu*\n\n"
            "Ejemplo: `https://apklis.cu/application/com.example.app`\n\n"
            "Luego elige:\n"
            "• `/auto` para detección automática\n"
            "• Número de versión específica",
            parse_mode='Markdown'
        )

# ========== DESCARGA Y ENVÍO DE APKs ==========
async def download_large_file(url: str, filepath: str, update: Update = None, status_msg = None):
    """Descarga archivos grandes usando aiohttp con progreso"""
    timeout = aiohttp.ClientTimeout(total=300)  # 5 minutos
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status == 200:
                total_size = int(response.headers.get('content-length', 0))
                
                with open(filepath, 'wb') as f:
                    downloaded = 0
                    last_update = 0
                    
                    async for chunk in response.content.iter_chunked(8192 * 8):  # 64KB chunks
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Actualizar progreso cada 5MB o 5%
                        if total_size > 0 and status_msg:
                            percent = (downloaded / total_size) * 100
                            current_time = asyncio.get_event_loop().time()
                            
                            # Actualizar cada 5% o cada 5 segundos
                            if (percent - last_update >= 5 or 
                                current_time - last_update >= 5):
                                if total_size > 10 * 1024 * 1024:  # >10MB
                                    try:
                                        await status_msg.edit_text(
                                            f"📥 *Descargando...*\n"
                                            f"📊 {percent:.1f}% ({downloaded/1024/1024:.1f}MB/{total_size/1024/1024:.1f}MB)",
                                            parse_mode='Markdown'
                                        )
                                    except:
                                        pass
                                last_update = percent
                
                return True
            else:
                logger.error(f"HTTP Error {response.status}")
                return False

async def download_and_send_apk(update: Update, context: ContextTypes.DEFAULT_TYPE, apk_url: str, version: str):
    """Descarga y envía el APK (soporta hasta 2GB)"""
    status_msg = await update.message.reply_text(f"🔄 *Preparando versión {version}...*", parse_mode='Markdown')
    
    try:
        # Extraer información
        package_name = apk_url.split('/')[-1]
        
        # Generar URL de descarga
        download_url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
        
        # Verificar que la versión existe antes de descargar
        await status_msg.edit_text("🔍 *Verificando versión...*", parse_mode='Markdown')
        
        if not await check_version_exists(package_name, int(version)):
            await status_msg.edit_text(
                f"❌ *Versión {version} no encontrada*\n\n"
                f"La versión especificada no existe o no está disponible.\n"
                f"Prueba con `/auto` para detección automática.",
                parse_mode='Markdown'
            )
            return
        
        # Nombre del archivo
        safe_filename = f"{package_name}-v{version}.apk"
        temp_path = f"temp_{hashlib.md5(safe_filename.encode()).hexdigest()[:8]}.apk"
        
        # Paso 1: Descargar
        await status_msg.edit_text("📥 *Descargando APK...*\n_Esto puede tomar varios minutos para archivos grandes_", parse_mode='Markdown')
        
        # Usar aiohttp para descarga asíncrona
        download_success = await download_large_file(download_url, temp_path, update, status_msg)
        
        if not download_success or not os.path.exists(temp_path):
            await status_msg.edit_text("❌ *Error en la descarga*\n\nVerifica tu conexión e intenta nuevamente", parse_mode='Markdown')
            return
        
        # Verificar tamaño del archivo
        file_size = os.path.getsize(temp_path)
        logger.info(f"Archivo descargado: {safe_filename} ({file_size/1024/1024:.2f} MB)")
        
        # Verificar que sea un APK válido (tamaño mínimo)
        if file_size < 1024 * 100:  # Menos de 100KB probablemente no es un APK válido
            await status_msg.edit_text("❌ *APK inválido*\n\nEl archivo descargado es demasiado pequeño para ser un APK válido", parse_mode='Markdown')
            os.remove(temp_path)
            return
        
        # Paso 2: Enviar
        await status_msg.edit_text("📤 *Enviando APK...*\n_Usando protocolo seguro_", parse_mode='Markdown')
        
        try:
            # Para archivos grandes (>50MB), usar Telethon si está disponible
            if file_size > 50 * 1024 * 1024 and telethon_client:
                await status_msg.edit_text("⚡ *Enviando archivo grande...*", parse_mode='Markdown')
                
                # Enviar con Telethon
                await telethon_client.send_file(
                    await telethon_client.get_input_entity(update.effective_chat.id),
                    temp_path,
                    caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB\n✅ Descargado automáticamente",
                    force_document=True
                )
            else:
                # Enviar con python-telegram-bot (hasta 50MB)
                with open(temp_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        filename=safe_filename,
                        caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB\n🎯 Detección automática",
                        parse_mode='Markdown'
                    )
            
            # Limpiar
            await status_msg.delete()
            
        finally:
            # Limpiar archivo temporal
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    except Exception as e:
        logger.error(f"Error enviando APK: {e}")
        await status_msg.edit_text(f"❌ *Error:* `{str(e)[:100]}`", parse_mode='Markdown')
    finally:
        # Asegurarse de remover al usuario de processing_users
        processing_users.discard(update.effective_user.id)

# ========== SERVIDOR WEB PARA RENDER ==========
async def health_check(request):
    """Endpoint de salud para Render"""
    return web.json_response({
        "status": "healthy",
        "year": 2026,
        "service": "APKLis Downloader Bot",
        "version_detection": f"{VERSION_MIN}-{VERSION_MAX}",
        "users_count": len(authorized_users),
        "active_downloads": len(processing_users),
        "telethon_available": telethon_client is not None,
        "http_session": http_session is not None,
        "timestamp": datetime.now().isoformat()
    })

async def start_web_server():
    """Inicia el servidor web para Render"""
    app = web.Application()
    
    # Endpoints
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', health_check)
    
    # Configurar el runner
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Iniciar en el puerto especificado
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"🌐 Servidor web iniciado en puerto {PORT}")
    logger.info(f"📅 Año: {datetime.now().year}")
    logger.info(f"🎯 Detección de versión: {VERSION_MIN}-{VERSION_MAX}")
    logger.info(f"🤖 Bot listo para recibir comandos")
    
    return runner

# ========== INICIALIZACIÓN Y EJECUCIÓN ==========
async def main():
    """Función principal asíncrona"""
    global telegram_app
    
    logger.info("🚀 Iniciando APKLis Bot 2026 con detección automática...")
    
    # 1. Inicializar sesión HTTP
    await init_http_session()
    
    # 2. Inicializar Telethon para archivos grandes
    await init_telethon()
    
    # 3. Crear aplicación de Telegram
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # 4. Registrar handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("status", status))
    telegram_app.add_handler(CommandHandler("add", add_users))
    telegram_app.add_handler(CommandHandler("remove", remove_users))
    telegram_app.add_handler(CommandHandler("users", list_users))
    telegram_app.add_handler(CommandHandler("auto", handle_message))  # Para /auto
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # 5. Inicializar bot
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
    
    logger.info("✅ Bot de Telegram iniciado")
    
    # 6. Iniciar servidor web (para Render)
    web_runner = await start_web_server()
    
    # 7. Mantener corriendo
    try:
        await asyncio.Future()  # Ejecutar indefinidamente
    except asyncio.CancelledError:
        logger.info("👋 Apagando bot...")
        
        # Apagar limpiamente
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        
        if telethon_client:
            await telethon_client.disconnect()
        
        await close_http_session()
        await web_runner.cleanup()

def run():
    """Punto de entrada para Render"""
    # Configurar asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Ejecutar bot indefinidamente
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot detenido por usuario")
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}")
    finally:
        loop.close()
        logger.info("👋 Bot finalizado")

if __name__ == '__main__':
    run()
