import os
import re
import asyncio
import logging
from typing import Dict, Set
from datetime import datetime
from aiohttp import web
import hashlib
import signal

# Pyrogram imports
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import BadRequest, FloodWait

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Variables de entorno
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 0))
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
PORT = int(os.getenv('PORT', 10000))
RENDER = os.getenv('RENDER', 'false').lower() == 'true'

# Validar variables críticas
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN no configurado")

# Almacenamiento en memoria
authorized_users: Set[int] = set()
if ADMIN_USER_ID:
    authorized_users.add(ADMIN_USER_ID)

processing_users: Set[int] = set()
user_sessions: Dict[int, Dict] = {}  # Almacena URLs pendientes por usuario

# URL base fija
FIXED_DOWNLOAD_ID = "d794ab9e-2e58-4ac9-97da-237b86d1a6c3"

# Variable global para la aplicación Pyrogram
app = None
web_runner = None

# ========== INICIALIZACIÓN PYROGRAM ==========
async def init_pyrogram():
    """Inicializa cliente Pyrogram"""
    global app
    
    try:
        app = Client(
            "apk_bot",
            api_id=API_ID if API_ID else None,
            api_hash=API_HASH if API_HASH else None,
            bot_token=BOT_TOKEN,
            parse_mode=ParseMode.MARKDOWN,
            max_concurrent_transmissions=3,
            sleep_threshold=60
        )
        
        # Registrar handlers
        register_handlers(app)
        
        await app.start()
        bot_info = await app.get_me()
        logger.info(f"✅ Bot iniciado: @{bot_info.username}")
        
    except Exception as e:
        logger.error(f"❌ Error iniciando Pyrogram: {e}")
        raise

# ========== REGISTRO DE HANDLERS ==========
def register_handlers(client: Client):
    """Registra todos los handlers del bot"""
    
    @client.on_message(filters.command("start"))
    async def start_handler(_, message: Message):
        user_id = message.from_user.id
        
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
                "• /status - Estado del bot\n"
                "• /clean - Limpiar caché"
            )
            await message.reply_text(welcome_msg)
        else:
            await message.reply_text(
                "🔒 *Acceso restringido*\n\n"
                "Contacta al administrador para acceder."
            )
    
    @client.on_message(filters.command("status"))
    async def status_handler(_, message: Message):
        user_id = message.from_user.id
        if user_id not in authorized_users:
            return
        
        status_msg = (
            f"📊 *Estado del Bot - {datetime.now().year}*\n\n"
            f"✅ Bot: Operativo\n"
            f"👥 Usuarios autorizados: {len(authorized_users)}\n"
            f"⏬ Descargas activas: {len(processing_users)}\n"
            f"📦 Sesiones en memoria: {len(user_sessions)}\n"
            f"🔧 API ID/HASH: {'✅' if API_ID and API_HASH else '❌'}\n"
            f"🌍 Render: {'✅' if RENDER else '❌'}"
        )
        await message.reply_text(status_msg)
    
    @client.on_message(filters.command("add"))
    async def add_handler(_, message: Message):
        user_id = message.from_user.id
        if user_id != ADMIN_USER_ID:
            await message.reply_text("❌ Solo administrador")
            return
        
        args = message.text.split()[1:]
        
        if not args:
            await message.reply_text(
                "📝 *Uso:* `/add id1 id2 id3`\n\n"
                "Ejemplo: `/add 123456789 987654321`"
            )
            return
        
        added = []
        for arg in args:
            if arg.isdigit():
                user_id_int = int(arg)
                if user_id_int not in authorized_users:
                    authorized_users.add(user_id_int)
                    added.append(str(user_id_int))
        
        if added:
            await message.reply_text(
                f"✅ *Usuarios añadidos:* {', '.join(added)}\n"
                f"Total: {len(authorized_users)} usuarios"
            )
        else:
            await message.reply_text("ℹ️ No se añadieron nuevos usuarios")
    
    @client.on_message(filters.command("remove"))
    async def remove_handler(_, message: Message):
        user_id = message.from_user.id
        if user_id != ADMIN_USER_ID:
            return
        
        args = message.text.split()[1:]
        
        if not args:
            await message.reply_text(
                "📝 *Uso:* `/remove id1 id2`\n\n"
                "Ejemplo: `/remove 123456789`"
            )
            return
        
        removed = []
        for arg in args:
            if arg.isdigit():
                user_id_int = int(arg)
                if user_id_int in authorized_users and user_id_int != ADMIN_USER_ID:
                    authorized_users.remove(user_id_int)
                    if user_id_int in user_sessions:
                        del user_sessions[user_id_int]
                    removed.append(str(user_id_int))
        
        if removed:
            await message.reply_text(f"❌ *Eliminados:* {', '.join(removed)}")
        else:
            await message.reply_text("ℹ️ No se eliminó ningún usuario")
    
    @client.on_message(filters.command("users"))
    async def users_handler(_, message: Message):
        user_id = message.from_user.id
        if user_id != ADMIN_USER_ID:
            return
        
        if not authorized_users:
            await message.reply_text("📭 No hay usuarios autorizados")
            return
        
        users_list = "\n".join([f"• `{uid}`" + (" 👑" if uid == ADMIN_USER_ID else "") for uid in authorized_users])
        await message.reply_text(
            f"👥 *Usuarios autorizados ({len(authorized_users)}):*\n\n{users_list}"
        )
    
    @client.on_message(filters.command("clean"))
    async def clean_handler(_, message: Message):
        user_id = message.from_user.id
        if user_id != ADMIN_USER_ID:
            return
        
        # Limpiar sesiones inactivas
        cleaned = 0
        current_time = datetime.now()
        for uid in list(user_sessions.keys()):
            # Eliminar sesiones con más de 30 minutos
            session = user_sessions[uid]
            if 'timestamp' in session:
                session_time = session['timestamp']
                if (current_time - session_time).total_seconds() > 1800:  # 30 minutos
                    del user_sessions[uid]
                    cleaned += 1
        
        await message.reply_text(f"🧹 *Cache limpiada*\nSesiones eliminadas: {cleaned}")
    
    @client.on_message(filters.text & ~filters.command)
    async def message_handler(_, message: Message):
        user_id = message.from_user.id
        
        # Verificar autorización
        if user_id not in authorized_users:
            await message.reply_text("🔒 No autorizado")
            return
        
        # Verificar si ya está procesando
        if user_id in processing_users:
            await message.reply_text("⏳ Tienes una descarga en curso. Espera...")
            return
        
        text = message.text.strip()
        
        # Buscar enlace APKLis
        apklis_pattern = r'https?://(?:www\.)?apklis\.cu/application/[a-zA-Z0-9._-]+'
        match = re.search(apklis_pattern, text)
        
        if match:
            # Guardar URL en sesión del usuario
            user_sessions[user_id] = {
                'apk_url': match.group(0),
                'timestamp': datetime.now()
            }
            await message.reply_text(
                "✅ *Enlace detectado*\n\n"
                "📤 *Envía el número de versión*\n"
                "Ejemplo: `34`"
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
                await download_and_send_apk(message, apk_url, version)
            except Exception as e:
                logger.error(f"Error en descarga: {e}")
                await message.reply_text(f"❌ Error: {str(e)[:200]}")
            finally:
                processing_users.discard(user_id)
        else:
            await message.reply_text(
                "📝 *Envía:*\n"
                "1. Un enlace de APKLis.cu\n"
                "2. El número de versión"
            )

# ========== DESCARGA Y ENVÍO DE APKs ==========
async def download_large_file(url: str, filepath: str) -> bool:
    """Descarga archivos grandes usando aiohttp"""
    import aiohttp
    
    timeout = aiohttp.ClientTimeout(total=600)  # 10 minutos máximo
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    total_size = int(response.headers.get('content-length', 0))
                    
                    with open(filepath, 'wb') as f:
                        downloaded = 0
                        async for chunk in response.content.iter_chunked(8192 * 16):  # 128KB chunks
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Log progreso para archivos grandes
                            if total_size > 10 * 1024 * 1024:  # >10MB
                                percent = (downloaded / total_size) * 100
                                if int(percent) % 20 == 0:  # Cada 20%
                                    logger.info(f"Descarga: {percent:.1f}% ({downloaded/1024/1024:.1f}MB/{total_size/1024/1024:.1f}MB)")
                    
                    return True
                else:
                    logger.error(f"HTTP Error {response.status} para URL: {url}")
                    return False
        except asyncio.TimeoutError:
            logger.error(f"Timeout descargando: {url}")
            return False
        except Exception as e:
            logger.error(f"Error en descarga: {e}")
            return False

async def download_and_send_apk(message: Message, apk_url: str, version: str):
    """Descarga y envía el APK"""
    status_msg = await message.reply_text("🔄 *Iniciando descarga...*")
    
    try:
        # Extraer información
        package_name = apk_url.split('/')[-1]
        
        # Generar URL de descarga
        download_url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version}.apk?download_id={FIXED_DOWNLOAD_ID}"
        
        # Nombre del archivo
        safe_filename = f"{package_name}-v{version}.apk"
        temp_path = f"temp_{hashlib.md5(safe_filename.encode()).hexdigest()[:8]}.apk"
        
        # Paso 1: Descargar
        await status_msg.edit_text("📥 *Descargando APK...*\n_Esto puede tomar varios minutos para archivos grandes_")
        
        # Usar aiohttp para descarga asíncrona
        download_success = await download_large_file(download_url, temp_path)
        
        if not download_success or not os.path.exists(temp_path):
            await status_msg.edit_text("❌ *Error en la descarga*\n\nVerifica:\n• Que la versión sea correcta\n• Que la aplicación exista")
            return
        
        # Verificar tamaño del archivo
        file_size = os.path.getsize(temp_path)
        logger.info(f"Archivo descargado: {safe_filename} ({file_size/1024/1024:.2f} MB)")
        
        # Paso 2: Enviar
        await status_msg.edit_text("📤 *Enviando APK...*\n_Usando protocolo seguro_")
        
        try:
            caption = f"📦 *{package_name}*\n🔢 Versión: {version}\n💾 Tamaño: {file_size/1024/1024:.1f}MB\n✅ Descargado con éxito"
            
            # Para archivos grandes, mostrar progreso
            progress_msg = None
            if file_size > 20 * 1024 * 1024:  # >20MB
                progress_msg = await message.reply_text("📤 Enviando archivo... (0%)")
                
                last_percent = 0
                def progress(current, total):
                    nonlocal last_percent
                    percent = (current / total) * 100
                    current_percent = int(percent)
                    if current_percent > last_percent and current_percent % 10 == 0:
                        asyncio.create_task(progress_msg.edit_text(f"📤 Enviando archivo... ({current_percent}%)"))
                        last_percent = current_percent
                
                await app.send_document(
                    chat_id=message.chat.id,
                    document=temp_path,
                    file_name=safe_filename,
                    caption=caption,
                    progress=progress
                )
                
                if progress_msg:
                    await progress_msg.delete()
            else:
                await app.send_document(
                    chat_id=message.chat.id,
                    document=temp_path,
                    file_name=safe_filename,
                    caption=caption
                )
            
            # Limpiar
            await status_msg.delete()
            
        except FloodWait as e:
            logger.warning(f"Flood wait: {e.value} segundos")
            await status_msg.edit_text(f"⏳ Demasiadas solicitudes. Espera {e.value} segundos...")
            await asyncio.sleep(e.value)
            await download_and_send_apk(message, apk_url, version)
            
        except BadRequest as e:
            logger.error(f"BadRequest: {e}")
            await status_msg.edit_text(f"❌ Error de Telegram: {str(e)[:100]}")
            
        finally:
            # Limpiar archivo temporal
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
                
    except Exception as e:
        logger.error(f"Error enviando APK: {e}")
        await status_msg.edit_text(f"❌ *Error:* `{str(e)[:100]}`")

# ========== SERVIDOR WEB PARA RENDER ==========
async def health_check(request):
    """Endpoint de salud para Render"""
    return web.json_response({
        "status": "healthy",
        "service": "APKLis Downloader Bot 2026",
        "bot_status": "running" if app else "stopped",
        "users_count": len(authorized_users),
        "active_downloads": len(processing_users),
        "timestamp": datetime.now().isoformat(),
        "year": 2026
    })

async def start_web_server():
    """Inicia el servidor web para Render"""
    global web_runner
    
    web_app = web.Application()
    web_app.router.add_get('/', health_check)
    web_app.router.add_get('/health', health_check)
    web_app.router.add_get('/status', health_check)
    
    web_runner = web.AppRunner(web_app)
    await web_runner.setup()
    
    site = web.TCPSite(web_runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"🌐 Servidor web iniciado en puerto {PORT}")
    return web_runner

# ========== MANEJO DE SEÑALES ==========
async def shutdown(signal=None):
    """Apagado limpio de la aplicación"""
    logger.info("🛑 Recibida señal de apagado...")
    
    # Detener Pyrogram
    if app and app.is_initialized:
        logger.info("👋 Deteniendo bot de Telegram...")
        await app.stop()
    
    # Detener servidor web
    if web_runner:
        logger.info("🌐 Deteniendo servidor web...")
        await web_runner.cleanup()
    
    logger.info("✅ Aplicación apagada correctamente")
    os._exit(0)

# ========== INICIALIZACIÓN Y EJECUCIÓN ==========
async def main():
    """Función principal asíncrona"""
    logger.info("🚀 Iniciando APKLis Bot 2026...")
    
    # Registrar manejadores de señales
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGABRT):
        signal.signal(sig, lambda s, f: asyncio.create_task(shutdown(s)))
    
    try:
        # 1. Inicializar Pyrogram
        await init_pyrogram()
        
        # 2. Iniciar servidor web (para Render)
        if RENDER:
            logger.info("🚀 Ejecutando en Render")
            await start_web_server()
        else:
            logger.info("💻 Ejecutando localmente")
        
        # 3. Información de estado
        logger.info(f"📊 Usuarios autorizados: {len(authorized_users)}")
        logger.info(f"🔧 API ID/HASH: {'Configurado' if API_ID and API_HASH else 'No configurado'}")
        logger.info(f"👑 Admin ID: {ADMIN_USER_ID}")
        
        # 4. Mantener la aplicación corriendo
        logger.info("✅ Bot listo y escuchando mensajes...")
        await idle()
        
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}")
        await shutdown()
    finally:
        await shutdown()

# ========== PUNTO DE ENTRADA ==========
if __name__ == '__main__':
    # Configurar asyncio para Render
    try:
        if RENDER:
            # En Render, usar el loop actual
            asyncio.run(main())
        else:
            # Localmente, crear nuevo loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot detenido por usuario")
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}")
