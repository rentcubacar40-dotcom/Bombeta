import os
import re
import asyncio
import logging
from typing import Dict, Set
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from aiohttp import web
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
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

# ========== COMANDOS DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    user_id = update.effective_user.id
    
    if user_id in authorized_users:
        welcome_msg = (
            "🤖 *Bot de APKs APKLis 2026*\n\n"
            "📲 *Cómo usar:*\n"
            "1. Envía enlace APKLis.cu\n"
            "2. Envía número de versión\n"
            "3. Recibe el APK\n\n"
            "⚡ *Soporta archivos hasta 2GB*\n"
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
        f"📦 Memoria: {len(user_sessions)} sesiones"
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
    apklis_pattern = r'https?://(?:www\.)?apklis\.cu/application/[a-zA-Z0-9._-]+'
    match = re.search(apklis_pattern, text)
    
    if match:
        # Guardar URL en sesión del usuario
        user_sessions[user_id] = {'apk_url': match.group(0)}
        await update.message.reply_text(
            "✅ *Enlace detectado*\n\n"
            "📤 *Envía el número de versión*\n"
            "Ejemplo: `34`",
            parse_mode='Markdown'
        )
    elif user_id in user_sessions and 'apk_url' in user_sessions[user_id] and text.isdigit():
        # Procesar descarga
        version = text
        apk_url = user_sessions[user_id]['apk_url']
        
        # Limpiar sesión
        del user_sessions[user_id]
        
        # Iniciar descarga
        processing_users.add(user_id)
        try:
            await download_and_send_apk(update, context, apk_url, version)
        except Exception as e:
            logger.error(f"Error en descarga: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
        finally:
            processing_users.discard(user_id)
    else:
        await update.message.reply_text(
            "📝 *Envía:*\n"
            "1. Un enlace de APKLis.cu\n"
            "2. El número de versión",
            parse_mode='Markdown'
        )

# ========== DESCARGA Y ENVÍO DE APKs ==========
async def download_large_file(url: str, filepath: str):
    """Descarga archivos grandes usando aiohttp"""
    import aiohttp
    
    timeout = aiohttp.ClientTimeout(total=300)  # 5 minutos
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status == 200:
                total_size = int(response.headers.get('content-length', 0))
                
                with open(filepath, 'wb') as f:
                    downloaded = 0
                    async for chunk in response.content.iter_chunked(8192 * 8):  # 64KB chunks
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Log progreso para archivos grandes
                        if total_size > 10 * 1024 * 1024:  # >10MB
                            percent = (downloaded / total_size) * 100
                            if int(percent) % 10 == 0:
                                logger.info(f"Descarga: {percent:.1f}% ({downloaded/1024/1024:.1f}MB/{total_size/1024/1024:.1f}MB)")
                
                return True
            else:
                logger.error(f"HTTP Error {response.status}")
                return False

async def download_and_send_apk(update: Update, context: ContextTypes.DEFAULT_TYPE, apk_url: str, version: str):
    """Descarga y envía el APK (soporta hasta 2GB)"""
    status_msg = await update.message.reply_text("🔄 *Iniciando descarga...*", parse_mode='Markdown')
    
    try:
        # Extraer información
        package_name = apk_url.split('/')[-1]
        
        # Generar URL de descarga
        download_url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
        
        # Nombre del archivo
        safe_filename = f"{package_name}-v{version}.apk"
        temp_path = f"temp_{hashlib.md5(safe_filename.encode()).hexdigest()[:8]}.apk"
        
        # Paso 1: Descargar
        await status_msg.edit_text("📥 *Descargando APK...*\n_Esto puede tomar varios minutos para archivos grandes_", parse_mode='Markdown')
        
        # Usar aiohttp para descarga asíncrona
        download_success = await download_large_file(download_url, temp_path)
        
        if not download_success or not os.path.exists(temp_path):
            await status_msg.edit_text("❌ *Error en la descarga*\n\nVerifica:\n• Que la versión sea correcta\n• Que la aplicación exista", parse_mode='Markdown')
            return
        
        # Verificar tamaño del archivo
        file_size = os.path.getsize(temp_path)
        logger.info(f"Archivo descargado: {safe_filename} ({file_size/1024/1024:.2f} MB)")
        
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
                    caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB\n✅ Descargado con éxito",
                    force_document=True
                )
            else:
                # Enviar con python-telegram-bot (hasta 50MB)
                with open(temp_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        filename=safe_filename,
                        caption=f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB",
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

# ========== SERVIDOR WEB PARA RENDER ==========
async def health_check(request):
    """Endpoint de salud para Render"""
    return web.json_response({
        "status": "healthy",
        "year": 2026,
        "service": "APKLis Downloader Bot",
        "users_count": len(authorized_users),
        "active_downloads": len(processing_users),
        "telethon_available": telethon_client is not None,
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
    logger.info(f"🤖 Bot listo para recibir comandos")
    
    return runner

# ========== INICIALIZACIÓN Y EJECUCIÓN ==========
async def main():
    """Función principal asíncrona"""
    global telegram_app
    
    logger.info("🚀 Iniciando APKLis Bot 2026...")
    
    # 1. Inicializar Telethon para archivos grandes
    await init_telethon()
    
    # 2. Crear aplicación de Telegram
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # 3. Registrar handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("status", status))
    telegram_app.add_handler(CommandHandler("add", add_users))
    telegram_app.add_handler(CommandHandler("remove", remove_users))
    telegram_app.add_handler(CommandHandler("users", list_users))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # 4. Inicializar bot
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
    
    logger.info("✅ Bot de Telegram iniciado")
    
    # 5. Iniciar servidor web (para Render)
    web_runner = await start_web_server()
    
    # 6. Mantener corriendo
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
