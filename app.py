from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials
import difflib
import re

app = Flask(__name__)

# ===================================
# 🔥 Inicializar Firebase
# ===================================
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()


# ===================================
# 📦 FUNCIÓN: Leer todo el inventario
# ===================================
def obtener_todo_stock():

    try:

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        creds = Credentials.from_service_account_file(
            "google_sheets_key.json",
            scopes=scopes
        )

        client = gspread.authorize(creds)

        sheet = client.open("stock_bot").sheet1

        rows = sheet.get_all_records()

        return rows

    except Exception as e:

        print("Error leyendo Google Sheets:", e)

        return []


# ===================================
# 🔎 FUNCIÓN: Detectar producto con tolerancia a errores
# ===================================
def detectar_producto_en_mensaje(mensaje, inventario):

    mensaje = mensaje.lower()

    mejor_match = None
    mejor_score = 0

    for item in inventario:

        producto = str(item.get("producto", "")).lower()

        # comparar nombre completo del producto
        score = difflib.SequenceMatcher(
            None,
            producto,
            mensaje
        ).ratio()

        if score > mejor_score:

            mejor_score = score
            mejor_match = item

        # también comparar palabra por palabra
        for palabra in producto.split():

            for palabra_usuario in mensaje.split():

                score = difflib.SequenceMatcher(
                    None,
                    palabra,
                    palabra_usuario
                ).ratio()

                if score > mejor_score:

                    mejor_score = score
                    mejor_match = item

    if mejor_score > 0.70:

        print("MEJOR MATCH:", mejor_match["producto"], "score:", mejor_score)

        return mejor_match

    return None

def detectar_varios_productos(mensaje, inventario):

    mensaje = mensaje.lower()
    palabras_usuario = mensaje.split()

    productos_encontrados = []

    for item in inventario:

        nombre_producto = str(item.get("producto", "")).lower()
        palabras_producto = nombre_producto.split()

        encontrado = False

        for palabra_producto in palabras_producto:

            # 1️⃣ match directo
            if palabra_producto in mensaje:
                encontrado = True
                break

            # 2️⃣ tolerancia a errores
            for palabra_usuario in palabras_usuario:

                similitud = difflib.SequenceMatcher(
                    None,
                    palabra_producto,
                    palabra_usuario
                ).ratio()

                if similitud > 0.75:
                    encontrado = True
                    break

            if encontrado:
                break

        if encontrado:
            productos_encontrados.append(item)

    return productos_encontrados



# ===================================
# 📦 FUNCIONES UNIVERSALES DE INVENTARIO
# ===================================
def listar_productos(inventario):

    productos = []

    for item in inventario:

        if "producto" in item:
            productos.append(item["producto"])

    return productos


def obtener_precio_producto(nombre, inventario):

    producto = detectar_producto_en_mensaje(nombre, inventario)

    if producto:
        return producto.get("precio")

    return None


def obtener_stock_producto(nombre, inventario):

    producto = detectar_producto_en_mensaje(nombre, inventario)

    if producto:
        return producto.get("stock")

    return None


# ===================================
# 🧠 FUNCIÓN: Obtener historial Firestore
# ===================================
def obtener_historial(phone, limite=6):

    mensajes_ref = db.collection("users") \
        .document(phone) \
        .collection("messages") \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(limite) \
        .stream()

    historial = []

    for msg in mensajes_ref:

        data = msg.to_dict()

        role = data.get("role")
        content = data.get("content")

        if role and content:

            historial.append(f"{role}: {content}")

    historial.reverse()

    return "\n".join(historial)

# ============================================================
# VERIFICAR SI ES LA PRIMERA VEZ QUE SE INICIA LA CONVERSACION
# ============================================================

def es_primera_vez(phone):

    mensajes_ref = db.collection("users") \
        .document(phone) \
        .collection("messages") \
        .limit(1) \
        .stream()

    return len(list(mensajes_ref)) == 0

# ===================================
# 🚗 GUARDAR AUTO ACTUAL DEL CLIENTE
# ===================================
def guardar_auto_actual(phone, producto):

    db.collection("users").document(phone).set(
        {"auto_actual": producto},
        merge=True
    )

# ===================================
# 🚗 OBTENER AUTO ACTUAL DEL CLIENTE
# ===================================
def obtener_auto_actual(phone):

    doc = db.collection("users").document(phone).get()

    if doc.exists:

        data = doc.to_dict()

        return data.get("auto_actual")

    return None

# ===================================
# 🤖 FUNCIÓN: Generar respuesta Ollama
# ===================================
def generar_respuesta_ollama(system_prompt, user_message):

    try:

        response = requests.post(
            "http://localhost:11434/api/chat",
            json={
                "model": "qwen2.5:7b",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "stream": False
            },
            timeout=60
        )

        response.raise_for_status()

        data = response.json()

        return data["message"]["content"]

    except Exception as e:

        print("Error con Ollama:", e)

        return "Hubo un problema generando la respuesta."
    
# ===================================
# 🧠 DETECTAR INTENCIÓN (IA)
# ===================================
def detectar_intencion(mensaje):

    prompt = f"""
Clasificá el siguiente mensaje en UNA de estas categorías:

- NUEVO: el usuario inicia una nueva consulta o no refiere a algo previo
- CONTEXTO: el usuario se refiere a algo ya mencionado antes

Mensaje: "{mensaje}"

Respondé SOLO con una palabra: NUEVO o CONTEXTO
"""

    try:
        response = requests.post(
            "http://localhost:11434/api/chat",
            json={
                "model": "qwen2.5:7b",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "stream": False
            },
            timeout=30
        )

        return response.json()["message"]["content"].strip().upper()

    except:
        return "NUEVO"


# ===================================
# 📩 WEBHOOK
# ===================================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():

    if request.method == "GET":
        return "OK", 200

    incoming_msg = request.values.get("Body", "").strip()

    phone = request.values.get("From", "")

    print(f"Mensaje recibido de {phone}: {incoming_msg}")

    # ===================================
    # 💾 Guardar mensaje usuario
    # ===================================
    db.collection("users").document(phone).collection("messages").add({
        "role": "user",
        "content": incoming_msg,
        "timestamp": datetime.now(timezone.utc)
    })

    # ===================================
    # 📦 Leer inventario
    # ===================================
    stock_data = obtener_todo_stock()

    stock_contexto = ""

    producto_detectado = detectar_producto_en_mensaje(incoming_msg, stock_data)

    if producto_detectado:

         stock_contexto += (
              f"\nProducto: {producto_detectado['producto']}\n"
              f"Stock disponible: {producto_detectado['stock']}\n"
              f"Precio: ${producto_detectado['precio']}\n"
              f"Descripción: {producto_detectado.get('descripcion','')}\n"
              f"Financiamiento: {producto_detectado.get('financiamiento','No disponible')}\n"
          )
    

    # ===================================
    # 🧠 Obtener historial
    # ===================================
    historial = obtener_historial(phone)

    # ===================================
    # 🧠 Construir prompt
    # ===================================
    prompt_completo = f"""
Sos Marcos, vendedor de autos con 15 años de experiencia en el mercado automotriz argentino. Trabajás en la concesionaria RezArrows y atendés clientes por WhatsApp de forma profesional, cercana y efectiva.
Tu objetivo es ayudar al cliente a encontrar el auto ideal y motivarlo a visitar la agencia.
PERSONALIDAD
Sos amable, cercano y confiable.
Hablás de forma natural argentina (ej: mirá, perfecto, bárbaro, te cuento, ¿cómo andás?).
No sonás robótico ni demasiado formal.
        
REGLA DE CONVERSACIÓN

Si es la primera vez que hablan presentate asi al inicio del mensaje: "Hola! como estas? Bienvenido a RezArrows, la mejor consecionaria y tu nueva oportunidad de cumplir tu proximo gran paso!"  
Si la conversación ya comenzó NO vuelvas a saludar ni presentarte nuevamente.
Respondé directamente a lo que dice el cliente.

Servicios que brindas:
Venta de autos nuevos y usados  
Financiamiento  
Permuta (como parte de pago)  
Gestión de patentamiento y transferencia  

STOCK DE AUTOS

El stock disponible se muestra más abajo en la sección:

"Información de inventario relevante"

Debés usar EXCLUSIVAMENTE esa información.

Nunca inventes autos ni datos.

FINANCIAMIENTO (REGLA CRÍTICA)

Si el usuario pregunta por financiación, cuotas, anticipo o planes:

- Debés responder EXCLUSIVAMENTE con la información de la sección "Financiamiento"
- NO podés inventar tasas, cuotas ni plazos
- NO podés usar conocimiento externo

Si no hay información de financiamiento disponible:

Decí:
"No contamos con información de financiamiento para este modelo actualmente."

IMPORTANTE SOBRE EL MODELO DETECTADO

El sistema puede detectar automáticamente el modelo correcto aunque el usuario lo haya escrito con errores.

Si en el mensaje aparece una etiqueta como:

[SISTEMA: El modelo correcto detectado...]

Ese es el modelo correcto del vehículo.

Debes usar ese modelo como referencia principal.


REGLAS SOBRE EL INVENTARIO

- No inventes precios.
- No inventes características.
- No inventes versiones.
- No inventes stock.

Si el modelo existe en el inventario, usá esos datos.

Si el modelo NO aparece en el inventario:

Decí únicamente:

"Actualmente no contamos con ese modelo en stock."

FORMA DE RESPONDER: Las respuestas deben ser CLARAS, CORTAS Y NATURALES, Y NO REPITAS FRASES DE RELLENO
        
CUANDO EL CLIENTE PREGUNTA POR UN AUTO: 
Respondé primero con los datos del inventario:
- unidades disponibles
- precio
- breve descripción
Luego podés hacer UNA pregunta breve para continuar la conversación.

Si el cliente pregunta por financiamiento:
Debés responder usando EXCLUSIVAMENTE la sección "Financiamiento" del inventario.
No inventes cuotas ni tasas.

Conversación previa:
{historial}

Información de inventario relevante:
{stock_contexto}

REGLA CRÍTICA

Si la sección de inventario está vacía significa que no se identificó el modelo del vehículo.

En ese caso pedile al cliente que indique el modelo del auto que busca.

Nunca inventes autos.

Usuario:
{incoming_msg}

Asistente:
"""

    # ===================================
    # 🤖 Generar respuesta IA
    # ===================================
    respuesta_ia = generar_respuesta_ollama(prompt_completo, incoming_msg)

    # ===================================
    # 💾 Guardar respuesta IA
    # ===================================
    db.collection("users").document(phone).collection("messages").add({
        "role": "assistant",
        "content": respuesta_ia,
        "timestamp": datetime.now(timezone.utc)
    })

    # ===================================
    # 📤 Responder
    # ===================================
    twilio_response = MessagingResponse()

    respuesta_corta = respuesta_ia[:1500]  # límite seguro

    twilio_response.message(respuesta_corta)

    print("RESPUESTA ENVIADA A WHATSAPP:", respuesta_corta)

    return str(twilio_response)


if __name__ == "__main__":
    app.run(port=5000, debug=True)
