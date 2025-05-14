import logging
import os
import json
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- IMPORTA LA NUOVA LIBRERIA ---
from telegramify_markdown import markdownify # Potrebbe essere .convert a seconda della versione/uso comune
                                            # Controlla la documentazione della libreria se markdownify non è il nome giusto.
                                            # Di solito si importa la funzione principale di conversione.
                                            # Guardando il README di sudoskys/telegramify-markdown,
                                            # l'uso tipico è: from telegramify_markdown import markdownify
                                            # e poi si chiama markdownify(tuo_testo_markdown)

# Configura il logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CARICAMENTO VARIABILI D'AMBIENTE ---
# (Codice invariato)
current_script_path = os.path.dirname(os.path.abspath(__file__))
project_root_path = os.path.dirname(current_script_path)
dotenv_path = os.path.join(project_root_path, '.env')

if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    logger.info(f"Variabili d'ambiente caricate da: {dotenv_path}")
else:
    dotenv_path_alternative = os.path.join(os.getcwd(), '.env')
    if os.path.exists(dotenv_path_alternative):
        load_dotenv(dotenv_path_alternative)
        logger.info(f"Variabili d'ambiente caricate da: {dotenv_path_alternative} (percorso alternativo)")
    else:
        logger.warning(f"File .env non trovato. Assicurati che esista e contenga le chiavi necessarie.")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAGAZZINO_API_ENDPOINT = os.getenv("MAGAZZINO_API_SEARCH_ENDPOINT", "http://localhost:5000/api/search/")
MAGAZZINO_API_KEY = os.getenv("MAGAZZINO_API_KEY")
# ---------------------------------------------

# --- RIMUOVI LA VECCHIA FUNZIONE escape_markdown_v2 SE PRESENTE ---
# def escape_markdown_v2(text: str) -> str: ... # ELIMINA QUESTA


# Funzione per il comando /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # (Codice invariato)
    user = update.effective_user
    await update.message.reply_html(
        f"Ciao {user.mention_html()}! Sono il bot del Magazzino del Creatore. 👋\n"
        "Inviami la tua domanda e cercherò una risposta nei contenuti indicizzati!",
    )
    logger.info(f"Utente {user.first_name} (ID: {user.id}) ha avviato il bot.")


# Funzione per gestire le domande
async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text
    user = update.effective_user
    logger.info(f"Domanda ricevuta da {user.first_name} (ID: {user.id}): {user_message}")

    thinking_message = await update.message.reply_text("Sto pensando... 🤔")

    if not MAGAZZINO_API_KEY: # (Controllo invariato)
        logger.error("La chiave API MAGAZZINO_API_KEY non è configurata nel .env!")
        await thinking_message.edit_text("Errore di configurazione del bot: la chiave API per il Magazzino non è impostata.")
        return

    headers = {"Content-Type": "application/json", "X-API-Key": MAGAZZINO_API_KEY}
    payload = {"query": user_message}

    try:
        logger.info(f"Invio richiesta a Magazzino API: {MAGAZZINO_API_ENDPOINT} con query: '{user_message}'")
        response = requests.post(MAGAZZINO_API_ENDPOINT, headers=headers, json=payload, timeout=60)
        response.raise_for_status()

        api_response_data = response.json()
        logger.info(f"Risposta ricevuta da Magazzino API (successo: {api_response_data.get('success')}, answer: {str(api_response_data.get('answer'))[:50]}...)")

        if api_response_data.get('success') and api_response_data.get('answer'):
            original_markdown_answer = api_response_data['answer']

            # --- USA telegramify-markdown PER CONVERTIRE ---
            try:
                # La funzione si chiama markdownify secondo il README della libreria
                telegram_formatted_answer = markdownify(original_markdown_answer)
                logger.info("Testo convertito con telegramify-markdown.")
            except Exception as e_telegramify:
                logger.error(f"Errore durante la conversione con telegramify-markdown: {e_telegramify}. Uso testo originale.")
                telegram_formatted_answer = original_markdown_answer # Fallback al testo originale
            # -----------------------------------------------

            if len(telegram_formatted_answer) > 4000:
                logger.warning(f"La risposta (telegramify) dell'LLM è molto lunga ({len(telegram_formatted_answer)} chars).")

            try:
                await thinking_message.edit_text(telegram_formatted_answer, parse_mode='MarkdownV2')
                logger.info("Risposta inviata a Telegram con MarkdownV2 (usando telegramify-markdown).")
            except Exception as e_telegram_markdown:
                logger.warning(f"Errore invio messaggio con MarkdownV2 (telegramify): {e_telegram_markdown}. Fallback testo semplice originale.")
                await thinking_message.edit_text(original_markdown_answer) # Fallback all'originale non processato da telegramify

        elif api_response_data.get('answer'):
            # (Gestione "BLOCKED" e altri errori invariata)
            if "BLOCKED" in api_response_data['answer'].upper():
                await thinking_message.edit_text(f"⚠️ La mia risposta è stata bloccata. Prova a riformulare la tua domanda. (Codice: {api_response_data.get('error_code', 'N/D')})")
            else:
                await thinking_message.edit_text(f"Non sono riuscito a formulare una risposta completa: {api_response_data['answer']}")

        else:
            # (Gestione errore API invariata)
            error_msg_from_api = api_response_data.get('message', 'Non ho trovato una risposta o si è verificato un errore.')
            logger.warning(f"Magazzino API ha risposto con successo '{api_response_data.get('success')}' ma senza un 'answer' valido. Messaggio: {error_msg_from_api}")
            await thinking_message.edit_text(f"Non sono riuscito a trovare una risposta. ({error_msg_from_api})")

    except requests.exceptions.HTTPError as http_err: # (Invariato)
        logger.error(f"Errore HTTP chiamando Magazzino API: {http_err}")
        error_message_detail = "Errore sconosciuto"
        try:
            error_content = http_err.response.json()
            error_message_detail = error_content.get("message", error_content.get("error", str(http_err)))
        except ValueError:
            error_message_detail = str(http_err.response.text)[:200]
        await thinking_message.edit_text(f"Oops! C'è stato un problema ({http_err.response.status_code}) nel contattare il Magazzino: {error_message_detail}")
    except requests.exceptions.ConnectionError as conn_err: # (Invariato)
        logger.error(f"Errore di connessione chiamando Magazzino API: {conn_err}")
        await thinking_message.edit_text("Non riesco a connettermi al Magazzino del Creatore in questo momento. Riprova più tardi.")
    except requests.exceptions.Timeout as timeout_err: # (Invariato)
        logger.error(f"Timeout chiamando Magazzino API: {timeout_err}")
        await thinking_message.edit_text("Il Magazzino del Creatore sta impiegando troppo tempo a rispondere. Riprova più tardi.")
    except Exception as e: # (Invariato)
        logger.error(f"Errore imprevisto durante la chiamata a Magazzino API: {e}", exc_info=True)
        await thinking_message.edit_text("Si è verificato un errore imprevisto. 😟")


def main() -> None: # (Invariato)
    logger.info("Avvio del bot...")
    if not TELEGRAM_BOT_TOKEN: logger.error("ERRORE: TELEGRAM_BOT_TOKEN non è impostata."); return
    if not MAGAZZINO_API_KEY: logger.error("ERRORE: MAGAZZINO_API_KEY non è impostata nel file .env.")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))
    logger.info("Bot avviato e in ascolto...")
    application.run_polling()
    logger.info("Bot terminato.")

if __name__ == '__main__':
    main()
