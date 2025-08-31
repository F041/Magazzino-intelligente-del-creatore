import logging
import os
import json
import re # Importa re per la sostituzione negli elenchi (se decidiamo di usarla)
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegramify_markdown import markdownify # Per convertire Markdown in formato Telegram

# Configura il logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CARICAMENTO VARIABILI D'AMBIENTE ---
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

# Funzione di escape per MarkdownV2 (utile se telegramify non escapa tutto per i link)
def escape_markdown_v2(text: str) -> str:
    if not text: return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{char}" if char in escape_chars else char for char in str(text))

# Funzione per il comando /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    logger.info(f"Domanda ricevuta da {user.first_name} (ID: {user.id}): '{user_message}'")

    thinking_message = await update.message.reply_text("Sto pensando... 🤔")

    if not MAGAZZINO_API_KEY:
        logger.error("MAGAZZINO_API_KEY non configurata!")
        try: await thinking_message.delete()
        except: pass
        await update.message.reply_text("Errore di configurazione del bot: API Key per il Magazzino mancante.")
        return

    headers = {"Content-Type": "application/json", "X-API-Key": MAGAZZINO_API_KEY}
    payload = {"query": user_message, "n_results": 10} # Aumentato n_results per avere più contesto per i riferimenti

    try:
        logger.info(f"!!! DEBUG CHIAVE BOT: La chiave API che sto per inviare e': '{MAGAZZINO_API_KEY}'")
        response = requests.post(MAGAZZINO_API_ENDPOINT, headers=headers, json=payload, timeout=90) # Timeout aumentato
        response.raise_for_status()
        api_response_data = response.json()
        logger.info(f"Risposta da Magazzino API: success={api_response_data.get('success')}, answer_len={len(str(api_response_data.get('answer')))}, refs_count={len(api_response_data.get('retrieved_results', []))}")

        try:
            await thinking_message.delete()
            logger.debug("Messaggio 'Sto pensando...' cancellato.")
        except Exception as e_delete:
            logger.warning(f"Impossibile cancellare 'Sto pensando...': {e_delete}")

        if api_response_data.get('success') and api_response_data.get('answer'):
            original_markdown_answer = str(api_response_data['answer']) # Assicura sia stringa

            # Formatta la risposta principale per Telegram
            try:
                telegram_main_answer_formatted = markdownify(original_markdown_answer)
            except Exception as e_telegramify_main:
                logger.error(f"Errore conversione risposta principale con telegramify-markdown: {e_telegramify_main}. Uso testo originale.")
                telegram_main_answer_formatted = original_markdown_answer

            final_message_to_send = telegram_main_answer_formatted

            # Aggiungi Riferimenti
            retrieved_results = api_response_data.get('retrieved_results', [])
            if retrieved_results:
                top_references = retrieved_results[:3]
                if top_references:
                    references_parts = ["\n\n\\-\\-\\-\n🔍 *Riferimenti:*"]
                    for i, ref in enumerate(top_references):
                        metadata = ref.get('metadata', {})
                        title = "N/D"; link = None; source_prefix = "Fonte Sconosciuta:"

                        if metadata.get('source_type') == 'video':
                            source_prefix = "*Video:*"
                            title = metadata.get('video_title', 'N/D')
                            if metadata.get('video_id'): link = f"https://www.youtube.com/watch?v={metadata['video_id']}"
                        elif metadata.get('source_type') == 'document':
                            source_prefix = "*Documento:*"
                            title = metadata.get('original_filename', 'N/D')
                        elif metadata.get('source_type') == 'article':
                            source_prefix = "*Articolo:*"
                            title = metadata.get('article_title', metadata.get('title', 'N/D'))
                            link = metadata.get('article_url', metadata.get('url'))

                        escaped_title = escape_markdown_v2(title) # Escapa il titolo

                        ref_line = f"\n{i+1}\\. {source_prefix} "
                        if link:
                            ref_line += f"[{escaped_title}]({link})"
                        else:
                            ref_line += escaped_title
                        references_parts.append(ref_line)

                    final_message_to_send += "".join(references_parts)

            if len(final_message_to_send) > 4096: # Limite Telegram
                logger.warning(f"Messaggio finale troppo lungo ({len(final_message_to_send)} chars). Invio solo risposta principale.")
                # Fallback: invia solo la risposta principale se il messaggio combinato è troppo lungo
                final_message_to_send = telegram_main_answer_formatted
                if len(final_message_to_send) > 4096: # Se anche la risposta principale è troppo lunga
                    final_message_to_send = final_message_to_send[:4090] + " \\.\\.\\." # Tronca e aggiungi puntini escapati

            logger.debug(f"Tentativo invio a Telegram (MarkdownV2):\n{final_message_to_send}")

            try:
                await update.message.reply_text(final_message_to_send, parse_mode='MarkdownV2')
            except Exception as e_send_tg:
                logger.warning(f"Errore invio messaggio formattato a Telegram ({e_send_tg}). Fallback a testo semplice (solo risposta).")
                await update.message.reply_text(original_markdown_answer) # Fallback estremo

        elif api_response_data.get('answer'):
            if "BLOCKED" in str(api_response_data['answer']).upper():
                 await update.message.reply_text(f"⚠️ La mia risposta è stata bloccata. Prova a riformulare. (Codice: {api_response_data.get('error_code', 'N/D')})")
            else:
                 await update.message.reply_text(f"Non sono riuscito a formulare una risposta completa: {api_response_data['answer']}")
        else:
            msg_api_err = api_response_data.get('message', 'Errore o nessuna risposta dal Magazzino.')
            logger.warning(f"Magazzino API success={api_response_data.get('success')}, no 'answer'. Msg: {msg_api_err}")
            await update.message.reply_text(f"Non ho trovato una risposta. ({msg_api_err})")

    except requests.exceptions.HTTPError as http_err:
        try: await thinking_message.delete()
        except: pass
        logger.error(f"Errore HTTP: {http_err.response.status_code} - {http_err.response.text[:200]}")
        await update.message.reply_text(f"Oops! Problema ({http_err.response.status_code}) contattando il Magazzino.")
    except requests.exceptions.RequestException as req_err: # Include ConnectionError, Timeout, etc.
        try: await thinking_message.delete()
        except: pass
        logger.error(f"Errore Richiesta (Connessione/Timeout): {req_err}")
        await update.message.reply_text("Non riesco a contattare il Magazzino. Riprova più tardi.")
    except Exception as e:
        try: await thinking_message.delete()
        except: pass
        logger.error(f"Errore imprevisto in handle_question: {e}", exc_info=True)
        await update.message.reply_text("Si è verificato un errore imprevisto. 😟")

def main() -> None:
    logger.info("Avvio del bot...")
    if not TELEGRAM_BOT_TOKEN: logger.error("ERRORE: TELEGRAM_BOT_TOKEN non impostata."); return
    if not MAGAZZINO_API_KEY: logger.error("ERRORE: MAGAZZINO_API_KEY non impostata."); return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))

    logger.info("Bot avviato e in ascolto...")
    application.run_polling()
    logger.info("Bot terminato.")

if __name__ == '__main__':
    main()
