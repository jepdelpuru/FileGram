🗂️ Telegram File Manager Bot

Un bot avanzado para gestionar archivos de tu PC directamente desde Telegram. Ideal para tener un control remoto privado y seguro de tu sistema Windows o Linux. Compatible con capturas de pantalla, navegación de carpetas, subida de archivos, ejecuciones remotas y más.

🚀 Características principales

💽 Navegación por unidades y carpetas

📂 Exploración de subcarpetas

📄 Vista y subida de archivos a Telegram (hasta 2 GB)

🖼️ Miniaturas automáticas para imágenes y vídeos

⏫ Progreso visual en tiempo real durante subidas/descargas

❌ Cancelación de tareas con un botón

🗑️ Eliminación de archivos con confirmación

⚙️ Ejecución de archivos desde Telegram

🖥️ Vista de pantalla en tiempo real (actualizable cada 5s)

📸 Captura de pantalla en alta calidad

📋 Listado de procesos activos (modo texto o documento)

🔐 Acceso limitado solo al propietario (configurable)

⚙️ Configuración

Obtén tu api_id, api_hash y bot_token desde my.telegram.org y BotFather.

Edita el script para rellenar estas variables:

OWNER_CHAT_ID = TU_ID_TELEGRAM  # Solo este ID podrá usar el bot
api_id = "TU_API_ID"
api_hash = "TU_API_HASH"
bot_token = "TU_BOT_TOKEN"

Ejecuta el bot:

python file_manager_bot.py

🧪 Comandos disponibles

/start - Inicia el bot y muestra las unidades disponibles.

🔒 Seguridad

El bot está protegido para ser usado solo por un usuario específico (el propietario). Si alguien más intenta usarlo, recibirá un mensaje de denegación.

