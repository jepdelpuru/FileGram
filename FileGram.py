import os
import shutil
import logging
import uuid
import datetime
import io
import time
import asyncio
import threading
import subprocess  # Nuevo para usar FFmpeg y para ejecutar archivos
import platform
import socket
import psutil
import mss
import mss.tools
import requests
from PIL import Image
import pyautogui
from pyrogram.types import InputMediaPhoto

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message
)

# Definir el chat id del propietario
OWNER_CHAT_ID = 123456789

# Decorador para restringir el acceso al OWNER_CHAT_ID
def owner_only(handler):
    async def wrapper(client, update):
        # Si es un CallbackQuery, usamos update.message.chat.id
        if isinstance(update, CallbackQuery):
            chat_id = update.message.chat.id if update.message else None
        # Si es un Message, usamos update.chat.id
        elif hasattr(update, "chat"):
            chat_id = update.chat.id
        else:
            chat_id = None
        if chat_id != OWNER_CHAT_ID:
            if hasattr(update, "reply"):
                await update.reply("No estás autorizado para usar este bot.")
            elif hasattr(update, "answer"):
                await update.answer("No estás autorizado para usar este bot.", show_alert=True)
            return
        return await handler(client, update)
    return wrapper

# Configuración del logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Diccionarios globales para mapear IDs a rutas y mensajes
FILE_MAP = {}
FOLDER_MAP = {}
CURRENT_MENU = {}       # {chat_id: message id} => mensaje actual del menú principal
NAV_MESSAGES = {}       # {chat_id: [message ids]} => mensajes de navegación
CANCEL_FLAGS = {}       # {identifier: threading.Event}
CURRENT_NAV_STATE = {}  # {chat_id: current path} => ruta actual (unidad o carpeta)
SCREENSHOT_TASKS = {}
FILE_MESSAGES = {}



def record_nav_message(chat_id: int, message_id: int):
    """Registra el id de un mensaje enviado para navegación en el chat."""
    if chat_id not in NAV_MESSAGES:
        NAV_MESSAGES[chat_id] = []
    NAV_MESSAGES[chat_id].append(message_id)

async def clear_nav_messages(client: Client, chat_id: int):
    """Borra todos los mensajes de navegación registrados en el chat."""
    if chat_id in NAV_MESSAGES and NAV_MESSAGES[chat_id]:
        try:
            await client.delete_messages(chat_id, NAV_MESSAGES[chat_id])
        except Exception as e:
            logger.warning(f"Error borrando mensajes de navegación: {e}")
        NAV_MESSAGES[chat_id] = []
    CURRENT_MENU[chat_id] = None
    CURRENT_NAV_STATE[chat_id] = None

def get_system_info():
    """Recopila información del sistema y la retorna en un formato bonito."""
    os_info = platform.platform()
    hostname = socket.gethostname()
    
    # Obtener direcciones IP locales de cada interfaz (IPv4)
    local_ips = []
    for interface_name, interface_addresses in psutil.net_if_addrs().items():
        for addr in interface_addresses:
            if addr.family == socket.AF_INET:
                local_ips.append(f"   • {interface_name}: {addr.address}")
    
    # Obtener IP pública (utilizando ipify)
    try:
        public_ip = requests.get("https://api.ipify.org", timeout=5).text
    except Exception:
        public_ip = "N/A"
    
    # Información del procesador (incluyendo frecuencia si es posible)
    processor = platform.processor()
    try:
        freq = psutil.cpu_freq().max
        processor += f" ({freq:.0f} MHz)"
    except Exception:
        pass
    
    # Memoria RAM total
    try:
        ram = psutil.virtual_memory().total
        ram_str = f"{ram / (1024 ** 3):.1f} GB"
    except Exception:
        ram_str = "N/A"
    
    info = (
        f"🖥️ Sistema: {os_info}\n"
        f"🏷️ Host: {hostname}\n"
        f"🌐 IP Pública: {public_ip}\n"
        f"📡 IPs Locales:\n" + "\n".join(local_ips) + "\n"
        f"⚙️ Procesador: {processor}\n"
        f"💾 RAM: {ram_str}"
    )
    return info

def format_size(size):
    """Convierte tamaño en bytes a una representación legible."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def list_drives():
    """Obtiene las unidades disponibles en el sistema."""
    drives = []
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:/"
            if os.path.exists(drive):
                drives.append(drive)
    else:
        drives.append("/")
    return drives

def is_image(file_path: str) -> bool:
    """Determina si el archivo es una imagen según su extensión."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]

def is_video(file_path: str) -> bool:
    """Determina si el archivo es un video según su extensión."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in [".mp4", ".avi", ".mkv", ".mov", ".wmv"]

def is_openable(file_path: str) -> bool:
    """Determina si el archivo se puede abrir (ejecutar) según su extensión."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in [".exe", ".bat", ".cmd", ".jpeg", ".jpg", ".png", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".mkv", ".avi"]

def generate_thumbnail(file_path: str, size=(300, 300)):
    """Genera una miniatura de la imagen y la retorna como BytesIO."""
    try:
        with Image.open(file_path) as im:
            im.thumbnail(size)
            bio = io.BytesIO()
            im.save(bio, format="JPEG", quality=95)
            bio.seek(0)
            return bio
    except Exception as e:
        logger.warning(f"Error generando miniatura para {file_path}: {e}")
        return None

def generate_video_thumbnail(file_path: str, size=(300, 300)):
    """
    Genera una miniatura para un video usando FFmpeg y la retorna como BytesIO.
    Se extrae un fotograma a 1 segundo, se escala a 'size' y se usa baja calidad (-q:v 31).
    Requiere que FFmpeg esté instalado en el sistema.
    """
    temp_thumb = f"{file_path}_thumb.jpg"
    try:
        command = [
            "ffmpeg",
            "-i", file_path,
            "-ss", "00:00:01.000",
            "-vframes", "1",
            "-vf", f"scale={size[0]}:{size[1]}",
            "-q:v", "31",  # Calidad baja
            temp_thumb
        ]
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        with open(temp_thumb, "rb") as f:
            data = f.read()
        os.remove(temp_thumb)
        bio = io.BytesIO(data)
        bio.seek(0)
        return bio
    except Exception as e:
        logger.warning(f"Error generando miniatura para video {file_path}: {e}")
        if os.path.exists(temp_thumb):
            os.remove(temp_thumb)
        return None

def navigation_markup(current_folder_id: str = None):
    """
    Devuelve un InlineKeyboardMarkup con botones de navegación:
      - Si current_folder_id existe y la carpeta tiene padre, se añade "⬅️ Atrás".
      - Siempre se incluye "🏠 Inicio".
    """
    buttons = []
    if current_folder_id:
        folder_path = FOLDER_MAP.get(current_folder_id)
        if folder_path:
            parent_path = os.path.dirname(folder_path)
            if parent_path and parent_path != folder_path:
                parent_id = str(uuid.uuid4())
                FOLDER_MAP[parent_id] = parent_path
                buttons.append(InlineKeyboardButton("⬅️ Atrás", callback_data=f"folder|{parent_id}"))
    buttons.append(InlineKeyboardButton("🏠 Inicio", callback_data="home"))
    return InlineKeyboardMarkup([buttons])


async def update_menu(client: Client, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup):
    """
    Actualiza el mensaje de menú actual del chat.
    Si ya existe, se edita; de lo contrario se envía un nuevo mensaje.
    Además, registra el mensaje en NAV_MESSAGES.
    """
    if chat_id in CURRENT_MENU and CURRENT_MENU[chat_id]:
        try:
            await client.edit_message_text(
                chat_id=chat_id,
                message_id=CURRENT_MENU[chat_id],
                text=text,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Error editando mensaje de menú: {e}")
            msg = await client.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup
            )
            CURRENT_MENU[chat_id] = msg.id
            record_nav_message(chat_id, msg.id)
    else:
        msg = await client.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup
        )
        CURRENT_MENU[chat_id] = msg.id
        record_nav_message(chat_id, msg.id)

async def main_panel(client: Client, message: Message):
    """
    Envía (o actualiza) el panel principal de unidades.
    Antes de mostrar el panel principal se borran todos los mensajes previos.
    """
    chat_id = message.chat.id
    await clear_nav_messages(client, chat_id)
    drives = list_drives()
    buttons = []
    for drive in drives:
        try:
            usage = shutil.disk_usage(drive)
            total = format_size(usage.total)
            free = format_size(usage.free)
        except Exception:
            total, free = "N/A", "N/A"
        text_drive = f"💽 {drive}\nTotal: {total}\nLibre: {free}"
        buttons.append([InlineKeyboardButton(text_drive, callback_data=f"drive|{drive}")])
    # Botón adicional para listar procesos activos
    buttons.append([InlineKeyboardButton("📋 Listar procesos activos", callback_data="list_processes")])
    # Botón para mostrar pantalla
    buttons.append([InlineKeyboardButton("🖥️ Mostrar pantalla en tiempo real", callback_data="show_screen")])
    buttons.append([InlineKeyboardButton("📸 Captura de pantalla de alta calidad", callback_data="upload_highres")])

    
    # Obtener información del sistema
    system_info = get_system_info()
    
    welcome_text = (
        "✨ ¡Bienvenido al Administrador de Archivos!\n\n" +
        system_info +
        "\n\nSelecciona una unidad:"
    )
    
    reply_markup = InlineKeyboardMarkup(buttons)  # Se eliminó el botón de limpiar chat
    await update_menu(client, message.chat.id, welcome_text, reply_markup)

# Función para actualizar el texto de un mensaje (para el progreso)
async def update_message_text(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error al actualizar el mensaje: {e}")

def make_upload_progress_hook(message: Message, loop, cancel_markup, cancel_flag, threshold: float = 5.0, min_interval: float = 3.0):
    """
    Función hook que actualiza el mensaje con una barra de progreso durante la subida.
    Si se activa el flag de cancelación, lanza una excepción para interrumpir la subida.
    """
    last_percentage = [0.0]
    last_update_time = [0.0]
    total_segments = 17

    def hook(current: int, total: int):
        try:
            if cancel_flag.is_set():
                raise Exception("Subida cancelada por el usuario.")
            percentage = current / total * 100
            now = time.time()
            if (abs(percentage - last_percentage[0]) >= threshold or percentage >= 100) and \
               (now - last_update_time[0] >= min_interval or percentage >= 100):
                last_percentage[0] = percentage
                last_update_time[0] = now
                filled = int(total_segments * percentage / 100)
                bar = "🟩" * filled + "⬜" * (total_segments - filled)
                new_text = f"⏫ Subiendo: {percentage:.2f}%\n{bar}"
                loop.call_soon_threadsafe(lambda: asyncio.create_task(update_message_text(message, new_text, reply_markup=cancel_markup)))
        except Exception as e:
            logger.error(f"Error en upload progress hook: {e}")
            raise e  # Para interrumpir la subida
    return hook

def make_download_progress_hook(message: Message, loop, cancel_markup, cancel_flag, threshold: float = 5.0, min_interval: float = 3.0):
    """
    Función hook que actualiza el mensaje con una barra de progreso durante la descarga.
    Si se activa el flag de cancelación, lanza una excepción para interrumpir la descarga.
    """
    last_percentage = [0.0]
    last_update_time = [0.0]
    total_segments = 17

    def hook(current: int, total: int):
        try:
            if cancel_flag.is_set():
                raise Exception("Descarga cancelada por el usuario.")
            percentage = current / total * 100
            now = time.time()
            if (abs(percentage - last_percentage[0]) >= threshold or percentage >= 100) and \
               (now - last_update_time[0] >= min_interval or percentage >= 100):
                last_percentage[0] = percentage
                last_update_time[0] = now
                filled = int(total_segments * percentage / 100)
                bar = "🟩" * filled + "⬜" * (total_segments - filled)
                new_text = f"⏳ Descargando: {percentage:.2f}%\n{bar}"
                loop.call_soon_threadsafe(lambda: asyncio.create_task(update_message_text(message, new_text, reply_markup=cancel_markup)))
        except Exception as e:
            logger.error(f"Error en download progress hook: {e}")
            raise e
    return hook

# ----------------------------------------------------------------
# Instanciamos el cliente antes de definir los handlers con decoradores
api_id = 123456789
api_hash = "123456789"
bot_token = "123456789"

# Instanciamos el cliente con la ruta segura de sesión
session_path = os.path.join(os.getenv("APPDATA"), "file_manager_bot")
app = Client(
    session_path,
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

# ----------------------------------------------------------------

@app.on_callback_query(filters.regex("^upload_highres$"))
@owner_only
async def upload_highres_callback(client: Client, query: CallbackQuery):
    await query.answer()
    chat_id = query.message.chat.id
    # Captura de pantalla en alta calidad usando mss y PIL
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # Selecciona el monitor principal
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.rgb)
        bio = io.BytesIO()
        img.save(bio, format="JPEG", quality=95)  # Alta calidad JPEG
        bio.seek(0)
        bio.name = "captura_alta.jpg"  # Asigna un nombre al buffer
    await client.send_document(
        chat_id=chat_id,
        document=bio,
        caption="📸 Captura de pantalla en alta calidad (documento)"
    )




@app.on_callback_query(filters.regex("^list_processes$"))
@owner_only
async def list_processes_callback(client: Client, query: CallbackQuery):
    await query.answer()
    try:
        # Ejecuta el comando "tasklist" y captura la salida en texto
        result = subprocess.run("tasklist", shell=True, capture_output=True, text=True)
        output = result.stdout
        
        # Si el mensaje es muy largo, se envía como archivo
        if len(output) > 4000:
            bio = io.BytesIO(output.encode())
            bio.name = "processes.txt"
            await client.send_document(
                chat_id=query.message.chat.id,
                document=bio,
                caption="Procesos activos"
            )
        else:
            await client.send_message(
                chat_id=query.message.chat.id,
                text=f"Procesos activos:\n\n{output}"
            )
    except Exception as e:
        await client.send_message(
            chat_id=query.message.chat.id,
            text=f"❌ Error al listar procesos: {e}"
        )

async def screen_update_task(client: Client, chat_id: int, message_id: int):
    try:
        while True:
            # Captura de pantalla usando mss
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # Selecciona el monitor principal
                sct_img = sct.grab(monitor)
                bio = io.BytesIO(mss.tools.to_png(sct_img.rgb, sct_img.size))
                bio.seek(0)
            # Actualiza el mensaje con la foto y un botón "Eliminar pantalla"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Eliminar pantalla", callback_data="stop_screen")]
            ])
            media = InputMediaPhoto(media=bio)
            await client.edit_message_media(chat_id=chat_id, message_id=message_id, media=media, reply_markup=keyboard)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error en actualización de pantalla: {e}")
        return


@app.on_callback_query(filters.regex("^show_screen$"))
@owner_only
async def show_screen_callback(client: Client, query: CallbackQuery):
    await query.answer()
    chat_id = query.message.chat.id
    # Si ya existe una tarea de captura en este chat, no se inicia una nueva
    if chat_id in SCREENSHOT_TASKS:
        await query.answer("La pantalla ya se está mostrando en tiempo real. Detén la actualización para volver a iniciarla.", show_alert=True)
        return
    # Captura la pantalla e inicia el proceso
    screenshot = pyautogui.screenshot()
    bio = io.BytesIO()
    screenshot.save(bio, format="JPEG", quality=95)
    bio.seek(0)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Eliminar pantalla", callback_data="stop_screen")]
    ])
    msg = await client.send_photo(chat_id, photo=bio, caption="⏳ Capturando pantalla...", reply_markup=keyboard)
    record_nav_message(chat_id, msg.id)
    task = asyncio.create_task(screen_update_task(client, chat_id, msg.id))
    SCREENSHOT_TASKS[chat_id] = task


async def screen_update_task(client: Client, chat_id: int, message_id: int):
    try:
        while True:
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # Selecciona el monitor principal
                sct_img = sct.grab(monitor)
                # Convertir la imagen capturada a una imagen PIL
                from PIL import Image
                img = Image.frombytes("RGB", sct_img.size, sct_img.rgb)
                # Guardar la imagen en un objeto BytesIO en formato JPEG con alta calidad
                bio = io.BytesIO()
                img.save(bio, format="JPEG", quality=95)
                bio.seek(0)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Eliminar pantalla", callback_data="stop_screen")]
            ])
            media = InputMediaPhoto(media=bio)
            await client.edit_message_media(chat_id=chat_id, message_id=message_id, media=media, reply_markup=keyboard)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error en actualización de pantalla: {e}")
        return



@app.on_callback_query(filters.regex("^stop_screen$"))
@owner_only
async def stop_screen_callback(client: Client, query: CallbackQuery):
    await query.answer("Deteniendo actualización")
    chat_id = query.message.chat.id
    # Cancela la tarea de actualización si existe
    if chat_id in SCREENSHOT_TASKS:
        SCREENSHOT_TASKS[chat_id].cancel()
        del SCREENSHOT_TASKS[chat_id]
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Error al borrar mensaje de pantalla: {e}")



@app.on_message(filters.command("start"))
@owner_only
async def start_handler(client: Client, message: Message):
    logger.info(f"Comando /start recibido de {message.chat.id}")
    await main_panel(client, message)

@app.on_callback_query(filters.regex("^home$"))
@owner_only
async def home_callback(client: Client, query: CallbackQuery):
    await query.answer()
    await clear_nav_messages(client, query.message.chat.id)
    await main_panel(client, query.message)

@app.on_callback_query(filters.regex(r"^drive\|"))
@owner_only
async def drive_callback(client: Client, query: CallbackQuery):
    await query.answer()
    _, drive = query.data.split("|", 1)
    chat_id = query.message.chat.id
    try:
        entries = os.listdir(drive)
    except Exception as e:
        await query.edit_message_text(text=f"❌ Error al acceder a la unidad {drive}: {e}")
        return

    folders = [d for d in entries if os.path.isdir(os.path.join(drive, d))]
    files = [f for f in entries if os.path.isfile(os.path.join(drive, f))]

    if not folders and not files:
        await query.edit_message_text(text=f"❌ La unidad {drive} está vacía.")
        return

    # Actualizamos el estado de navegación con la unidad seleccionada
    CURRENT_NAV_STATE[chat_id] = drive
    buttons = []
    # Agregamos los botones para las carpetas (si existen)
    for d in folders:
        full_path = os.path.join(drive, d)
        folder_id = str(uuid.uuid4())
        FOLDER_MAP[folder_id] = full_path
        buttons.append([InlineKeyboardButton(f"📁 {d}", callback_data=f"folder|{folder_id}")])
    # Si existen archivos, agregamos un botón para listarlos
    if files:
        drive_id = str(uuid.uuid4())
        FOLDER_MAP[drive_id] = drive  # Usamos la unidad misma como "carpeta" para listar archivos
        buttons.append([InlineKeyboardButton("📄 Listar archivos", callback_data=f"list_files|{drive_id}|0")])
    reply_markup = InlineKeyboardMarkup(buttons)
    text = f"📂 Unidad: {drive}\nSelecciona una carpeta o lista los archivos disponibles."
    await update_menu(client, chat_id, text, reply_markup)

@app.on_callback_query(filters.regex(r"^folder\|"))
@owner_only
async def folder_callback(client: Client, query: CallbackQuery):
    await query.answer()
    _, folder_id = query.data.split("|", 1)
    chat_id = query.message.chat.id
    folder_path = FOLDER_MAP.get(folder_id)
    if not folder_path:
        await update_menu(client, chat_id, "❌ Carpeta no encontrada.", navigation_markup())
        return
    # Actualizamos el estado de navegación con la carpeta seleccionada
    CURRENT_NAV_STATE[chat_id] = folder_path
    try:
        items = os.listdir(folder_path)
    except Exception as e:
        await update_menu(client, chat_id, f"❌ Error al acceder a la carpeta {folder_path}: {e}", navigation_markup(folder_id))
        return
    subfolders = [d for d in items if os.path.isdir(os.path.join(folder_path, d))]
    files = [f for f in items if os.path.isfile(os.path.join(folder_path, f))]
    msg = (
        f"📁 Carpeta: {folder_path}\n"
        f"📂 Subcarpetas: {len(subfolders)}\n"
        f"📄 Archivos: {len(files)}"
    )
    other_buttons = []
    if subfolders:
        other_buttons.append([InlineKeyboardButton("📂 Listar subcarpetas", callback_data=f"list_subfolders|{folder_id}|0")])
    if files:
        other_buttons.append([InlineKeyboardButton("📄 Listar archivos", callback_data=f"list_files|{folder_id}|0")])
    nav_markup = navigation_markup(folder_id)
    combined_buttons = nav_markup.inline_keyboard + other_buttons
    full_markup = InlineKeyboardMarkup(combined_buttons)
    await update_menu(client, chat_id, msg, full_markup)

@app.on_callback_query(filters.regex(r"^list_files\|"))
@owner_only
async def list_files_callback(client: Client, query: CallbackQuery):
    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 3:
        await update_menu(client, query.message.chat.id, "❌ Parámetros inválidos.", navigation_markup())
        return
    _, folder_id, page_str = parts
    chat_id = query.message.chat.id
    folder_path = FOLDER_MAP.get(folder_id)
    if not folder_path:
        await update_menu(client, chat_id, "❌ Carpeta no encontrada.", navigation_markup())
        return
    try:
        files = sorted(
            [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))],
            key=lambda f: os.path.getctime(os.path.join(folder_path, f)),
            reverse=True,
        )
    except Exception as e:
        await update_menu(client, chat_id, f"❌ Error al listar archivos en {folder_path}: {e}", navigation_markup(folder_id))
        return
    if not files:
        await update_menu(client, chat_id, f"❌ No hay archivos en la carpeta {folder_path}.", navigation_markup(folder_id))
        return
    page = int(page_str)
    per_page = 10
    start_index = page * per_page
    end_index = start_index + per_page
    page_files = files[start_index:end_index]
    for f in page_files:
        full_path = os.path.join(folder_path, f)
        try:
            file_stat = os.stat(full_path)
            creation_date = datetime.datetime.fromtimestamp(file_stat.st_ctime).strftime("%d/%m/%Y %H:%M:%S")
            size = format_size(file_stat.st_size)
        except Exception:
            creation_date = "N/A"
            size = "N/A"
        msg = (
            f"📄 Archivo: {f}\n"
            f"📅 Creación: {creation_date}\n"
            f"💾 Tamaño: {size}"
        )
        file_id = str(uuid.uuid4())
        FILE_MAP[file_id] = full_path
        # Botón de subir
        markup = None
        if file_stat.st_size < 2 * 1024 * 1024 * 1024:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬆️ Subir a Telegram", callback_data=f"upload|{file_id}")]])
        # Botón de eliminar
        delete_button = InlineKeyboardButton("🗑️ Eliminar", callback_data=f"delete|{file_id}")
        if markup:
            markup.inline_keyboard.append([delete_button])
        else:
            markup = InlineKeyboardMarkup([[delete_button]])
        # Botón de ejecutar para archivos ejecutables
        if is_openable(full_path):
            exec_button = InlineKeyboardButton("Ejecutar/Abrir", callback_data=f"execute|{file_id}")
            markup.inline_keyboard.append([exec_button])

        # Envío según tipo de archivo
        if is_image(full_path):
            thumbnail = generate_thumbnail(full_path)
            if thumbnail:
                sent = await client.send_photo(
                    chat_id=chat_id,
                    photo=thumbnail,
                    caption=msg,
                    reply_markup=markup
                )
                record_nav_message(chat_id, sent.id)
                thumbnail.close()
            else:
                sent = await client.send_message(
                    chat_id=chat_id,
                    text=msg + "\n❌ No se pudo generar la miniatura.",
                    reply_markup=markup
                )
                record_nav_message(chat_id, sent.id)
        elif is_video(full_path):
            thumbnail = generate_video_thumbnail(full_path)
            if thumbnail:
                sent = await client.send_photo(
                    chat_id=chat_id,
                    photo=thumbnail,
                    caption=msg,
                    reply_markup=markup
                )
                record_nav_message(chat_id, sent.id)
                thumbnail.close()
            else:
                sent = await client.send_message(
                    chat_id=chat_id,
                    text=msg + "\n❌ No se pudo generar la miniatura del video.",
                    reply_markup=markup
                )
                record_nav_message(chat_id, sent.id)
        else:
            sent = await client.send_message(
                chat_id=chat_id,
                text=msg,
                reply_markup=markup
            )
            record_nav_message(chat_id, sent.id)
    nav_buttons = [InlineKeyboardButton("🏠 Inicio", callback_data="home")]
    if end_index < len(files):
        nav_buttons.append(InlineKeyboardButton("▶️ Siguientes 10", callback_data=f"list_files|{folder_id}|{page+1}"))
    nav_markup = InlineKeyboardMarkup([nav_buttons])
    nav_msg = await client.send_message(chat_id=chat_id, text="Navegación:", reply_markup=nav_markup)
    record_nav_message(chat_id, nav_msg.id)

@app.on_callback_query(filters.regex(r"^list_subfolders\|"))
@owner_only
async def list_subfolders_callback(client: Client, query: CallbackQuery):
    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 2:
        await update_menu(client, query.message.chat.id, "❌ Error en los parámetros del callback.", navigation_markup())
        return
    _, folder_id, _ = parts
    chat_id = query.message.chat.id
    folder_path = FOLDER_MAP.get(folder_id)
    if not folder_path:
        await update_menu(client, chat_id, "❌ Carpeta no encontrada.", navigation_markup())
        return
    try:
        subfolders = [d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))]
    except Exception as e:
        await update_menu(client, chat_id, f"❌ Error al acceder a la carpeta {folder_path}: {e}", navigation_markup(folder_id))
        return
    if not subfolders:
        await update_menu(client, chat_id, f"❌ No hay subcarpetas en {folder_path}.", navigation_markup(folder_id))
        return
    buttons = []
    for d in subfolders:
        full_path = os.path.join(folder_path, d)
        subfolder_id = str(uuid.uuid4())
        FOLDER_MAP[subfolder_id] = full_path
        buttons.append([InlineKeyboardButton(f"📁 {d}", callback_data=f"folder|{subfolder_id}")])
    nav_markup = navigation_markup(folder_id)
    combined_buttons = nav_markup.inline_keyboard + buttons
    full_markup = InlineKeyboardMarkup(combined_buttons)
    await update_menu(client, chat_id, f"Subcarpetas en {folder_path}:", full_markup)

@app.on_callback_query(filters.regex(r"^upload\|"))
@owner_only
async def upload_callback(client: Client, query: CallbackQuery):
    await query.answer()
    chat_id = query.message.chat.id
    _, file_key = query.data.split("|", 1)
    file_path = FILE_MAP.get(file_key)
    if not file_path:
        await query.edit_message_text(text="❌ Referencia inválida para el archivo.")
        return
    if not os.path.exists(file_path):
        await query.edit_message_text(text="❌ El archivo no existe en el servidor.")
        return
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"cancel|{file_key}")]])
    upload_msg = await client.send_message(chat_id=chat_id, text="⏳ Subiendo archivo, por favor espere...", reply_markup=cancel_markup)
    record_nav_message(chat_id, upload_msg.id)
    cancel_flag = threading.Event()
    CANCEL_FLAGS[file_key] = cancel_flag
    try:
        # Dentro del try en el handler "upload_callback"
        file_size = os.path.getsize(file_path)
        if file_size > 2 * 1024 * 1024 * 1024:
            await client.send_message(chat_id=chat_id, text="❌ El archivo supera el límite permitido de tamaño.")
            return
        loop = asyncio.get_running_loop()
        progress_hook = make_upload_progress_hook(upload_msg, loop, cancel_markup, cancel_flag)
        if is_image(file_path):
            await client.send_photo(
                chat_id=chat_id,
                photo=file_path,
                progress=progress_hook
            )
        elif is_video(file_path):
            await client.send_video(
                chat_id=chat_id,
                video=file_path,
                progress=progress_hook
            )
        else:
            await client.send_document(
                chat_id=chat_id,
                document=file_path,
                progress=progress_hook
            )

    except Exception as e:
        err_msg = str(e)
        if "Subida cancelada por el usuario" in err_msg or "NoneType" in err_msg:
            try:
                await upload_msg.delete()
            except Exception as delete_err:
                logger.warning(f"Error borrando mensaje de progreso cancelado: {delete_err}")
            await query.answer("Subida cancelada", show_alert=True)
        else:
            await query.edit_message_text(text=f"❌ Error al subir el archivo:\n{file_path}\n{e}")
            logger.error(f"Error al subir el archivo: {e}")
        return
    finally:
        CANCEL_FLAGS.pop(file_key, None)


# Handler para eliminar archivos: muestra mensaje de confirmación
@app.on_callback_query(filters.regex(r"^delete\|"))
@owner_only
async def delete_file_prompt(client: Client, query: CallbackQuery):
    await query.answer()
    _, file_id = query.data.split("|", 1)
    confirm_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Sí", callback_data=f"confirm_delete|{file_id}")],
        [InlineKeyboardButton("No", callback_data=f"cancel_delete|{file_id}")]
    ])
    confirm_msg = await client.send_message(query.message.chat.id, "¿Estás seguro de eliminar este archivo?", reply_markup=confirm_markup)
    record_nav_message(query.message.chat.id, confirm_msg.id)

# Handler para confirmar la eliminación
@app.on_callback_query(filters.regex(r"^confirm_delete\|"))
@owner_only
async def confirm_delete_handler(client: Client, query: CallbackQuery):
    await query.answer()
    _, file_id = query.data.split("|", 1)
    file_path = FILE_MAP.get(file_id)
    if not file_path:
        await query.edit_message_text("❌ Archivo no encontrado.")
        return
    try:
        os.remove(file_path)
        del FILE_MAP[file_id]
        await query.edit_message_text("✅ Archivo eliminado.")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al eliminar el archivo: {e}")

# Handler para cancelar la eliminación
@app.on_callback_query(filters.regex(r"^cancel_delete\|"))
@owner_only
async def cancel_delete_handler(client: Client, query: CallbackQuery):
    await query.answer("Operación cancelada", show_alert=True)
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Error borrando mensaje de confirmación: {e}")

# Handler para ejecutar archivos ejecutables: muestra mensaje de confirmación
@app.on_callback_query(filters.regex(r"^execute\|"))
@owner_only
async def execute_file_prompt(client: Client, query: CallbackQuery):
    await query.answer()
    _, file_id = query.data.split("|", 1)
    confirm_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Sí", callback_data=f"confirm_execute|{file_id}")],
        [InlineKeyboardButton("No", callback_data=f"cancel_execute|{file_id}")]
    ])
    confirm_msg = await client.send_message(query.message.chat.id, "¿Estás seguro de ejecutar este archivo?", reply_markup=confirm_markup)
    record_nav_message(query.message.chat.id, confirm_msg.id)

# Handler para confirmar la ejecución
@app.on_callback_query(filters.regex(r"^confirm_execute\|"))
@owner_only
async def confirm_execute_handler(client: Client, query: CallbackQuery):
    await query.answer()
    _, file_id = query.data.split("|", 1)
    file_path = FILE_MAP.get(file_id)
    if not file_path:
        await query.edit_message_text("❌ Archivo no encontrado.")
        return
    try:
        # Ejecutar el archivo. Se utiliza shell=True para archivos .bat o .cmd.
        subprocess.Popen([file_path], shell=True)
        await query.edit_message_text("✅ Archivo ejecutado.")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al ejecutar el archivo: {e}")

# Handler para cancelar la ejecución
@app.on_callback_query(filters.regex(r"^cancel_execute\|"))
@owner_only
async def cancel_execute_handler(client: Client, query: CallbackQuery):
    await query.answer("Operación cancelada", show_alert=True)
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Error borrando mensaje de confirmación de ejecución: {e}")

@app.on_callback_query(filters.regex(r"^cancel\|"))
@owner_only
async def cancel_upload_callback(client: Client, query: CallbackQuery):
    _, file_key = query.data.split("|", 1)
    cancel_flag = CANCEL_FLAGS.get(file_key)
    if cancel_flag:
        cancel_flag.set()
        await query.answer("Subida cancelada", show_alert=True)
    else:
        await query.answer("No hay una subida activa para cancelar", show_alert=True)

# Handler para fotos (imágenes enviadas en modo photo)
@app.on_message(filters.photo)
@owner_only
async def handle_photo_upload(client: Client, message: Message):
    chat_id = message.chat.id
    current_path = CURRENT_NAV_STATE.get(chat_id)
    if not current_path:
        await message.reply("No estás en ninguna carpeta activa. Navega a una unidad o carpeta primero.")
        return
    file_name = f"{message.photo.file_id}.jpg"
    dest_path = os.path.join(current_path, file_name)
    
    download_id = str(uuid.uuid4())
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"cancel_download|{download_id}")]])
    progress_msg = await message.reply("⏳ Descargando foto, por favor espere...", reply_markup=cancel_markup)
    record_nav_message(chat_id, progress_msg.id)
    
    cancel_flag = threading.Event()
    CANCEL_FLAGS[download_id] = cancel_flag

    loop = asyncio.get_running_loop()
    progress_hook = make_download_progress_hook(progress_msg, loop, cancel_markup, cancel_flag)
    
    try:
        await message.download(file_name=dest_path, progress=progress_hook)
    except Exception as e:
        if "Descarga cancelada por el usuario" in str(e):
            try:
                await progress_msg.delete()
            except Exception as delete_err:
                logger.warning(f"Error borrando mensaje de progreso cancelado: {delete_err}")
            await message.reply("Descarga cancelada")
        else:
            await message.reply(f"❌ Error al descargar la foto: {e}")
        return
    finally:
        CANCEL_FLAGS.pop(download_id, None)
    try:
        await progress_msg.delete()
    except Exception as e_del:
        logger.warning(f"Error borrando mensaje de progreso: {e_del}")
    await message.reply(f"✅ Foto descargada en:\n{dest_path}")

@app.on_callback_query(filters.regex(r"^cancel_download\|"))
@owner_only
async def cancel_download_callback(client: Client, query: CallbackQuery):
    _, short_id = query.data.split("|", 1)
    cancel_flag = CANCEL_FLAGS.get(short_id)
    if cancel_flag:
        cancel_flag.set()
        await query.answer("Descarga cancelada", show_alert=True)
    else:
        await query.answer("No hay una descarga activa para cancelar", show_alert=True)

@app.on_message(filters.document)
@owner_only
async def handle_file_upload(client: Client, message: Message):
    chat_id = message.chat.id
    current_path = CURRENT_NAV_STATE.get(chat_id)
    if not current_path:
        await message.reply("No estás en ninguna carpeta activa. Navega a una unidad o carpeta primero.")
        return
    file_name = message.document.file_name
    dest_path = os.path.join(current_path, file_name)
    
    # Genera un identificador único para este archivo y almacena la referencia al mensaje original
    doc_key = str(uuid.uuid4())
    FILE_MESSAGES[doc_key] = message
    
    # Verificar si el archivo ya existe
    if os.path.exists(dest_path):
        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Sobreescribir", callback_data=f"overwrite|{doc_key}|{file_name}")],
            [InlineKeyboardButton("Renombrar", callback_data=f"rename|{doc_key}|{file_name}")]
        ])
        await message.reply(f"El archivo '{file_name}' ya existe. ¿Deseas sobreescribirlo o renombrarlo?", reply_markup=confirm_markup)
        return
    
    # Si no existe, proceder normalmente con la descarga
    download_id = str(uuid.uuid4())
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"cancel_download|{download_id}")]])
    progress_msg = await message.reply("⏳ Descargando archivo, por favor espere...", reply_markup=cancel_markup)
    record_nav_message(chat_id, progress_msg.id)
    cancel_flag = threading.Event()
    CANCEL_FLAGS[download_id] = cancel_flag

    loop = asyncio.get_running_loop()
    progress_hook = make_download_progress_hook(progress_msg, loop, cancel_markup, cancel_flag)
    try:
        await message.download(file_name=dest_path, progress=progress_hook)
    except Exception as e:
        if "Descarga cancelada por el usuario" in str(e):
            try:
                await progress_msg.delete()
            except Exception as delete_err:
                logger.warning(f"Error borrando mensaje de progreso cancelado: {delete_err}")
            await message.reply("Descarga cancelada")
        else:
            await message.reply(f"❌ Error al descargar el archivo: {e}")
        return
    finally:
        CANCEL_FLAGS.pop(download_id, None)
    try:
        await progress_msg.delete()
    except Exception as e_del:
        logger.warning(f"Error borrando mensaje de progreso: {e_del}")
    await message.reply(f"✅ Archivo descargado en:\n{dest_path}")
    # Elimina la referencia, ya que se usó
    if doc_key in FILE_MESSAGES:
        del FILE_MESSAGES[doc_key]


@app.on_callback_query(filters.regex(r"^(overwrite|rename)\|"))
@owner_only
async def handle_overwrite_rename(client: Client, query: CallbackQuery):
    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 3:
        await query.edit_message_text("Datos incompletos.")
        return
    action = parts[0]  # "overwrite" o "rename"
    doc_key = parts[1]
    file_name = parts[2]
    chat_id = query.message.chat.id
    current_path = CURRENT_NAV_STATE.get(chat_id)
    if not current_path:
        await query.edit_message_text("No estás en ninguna carpeta activa.")
        return
    dest_path = os.path.join(current_path, file_name)
    
    # Obtén el mensaje original que contiene el archivo
    original_message = FILE_MESSAGES.get(doc_key)
    if not original_message:
        await query.edit_message_text("❌ No se encontró la referencia del archivo original.")
        return

    if action == "overwrite":
        try:
            os.remove(dest_path)
        except Exception as e:
            await query.edit_message_text(f"❌ Error al eliminar el archivo existente: {e}")
            return
    elif action == "rename":
        base, ext = os.path.splitext(file_name)
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        new_file_name = f"{base}_{timestamp}{ext}"
        dest_path = os.path.join(current_path, new_file_name)

    # Proceder a descargar usando el mensaje original
    download_id = str(uuid.uuid4())
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"cancel_download|{download_id}")]])
    progress_msg = await client.send_message(chat_id, "⏳ Descargando archivo, por favor espere...", reply_markup=cancel_markup)
    record_nav_message(chat_id, progress_msg.id)
    cancel_flag = threading.Event()
    CANCEL_FLAGS[download_id] = cancel_flag
    loop = asyncio.get_running_loop()
    progress_hook = make_download_progress_hook(progress_msg, loop, cancel_markup, cancel_flag)
    try:
        await original_message.download(file_name=dest_path, progress=progress_hook)
    except Exception as e:
        if "Descarga cancelada por el usuario" in str(e):
            try:
                await progress_msg.delete()
            except Exception as delete_err:
                logger.warning(f"Error borrando mensaje de progreso cancelado: {delete_err}")
            await client.send_message(chat_id, "Descarga cancelada")
        else:
            await client.send_message(chat_id, f"❌ Error al descargar el archivo: {e}")
        return
    finally:
        CANCEL_FLAGS.pop(download_id, None)
    try:
        await progress_msg.delete()
    except Exception as e_del:
        logger.warning(f"Error borrando mensaje de progreso: {e_del}")
    await client.send_message(chat_id, f"✅ Archivo descargado en:\n{dest_path}")
    # Elimina el mensaje de confirmación y la referencia original
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Error borrando mensaje de confirmación: {e}")
    if doc_key in FILE_MESSAGES:
        del FILE_MESSAGES[doc_key]


if __name__ == "__main__":
    logger.info("Bot en ejecución...")
    app.run()
