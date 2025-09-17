# FILE: app/api/routes/statistics.py

import logging
import sqlite3
import os
import textstat # Importiamo la nuova libreria
from flask import Blueprint, render_template, current_app, jsonify
from flask_login import login_required, current_user
import json # Ci servirà per passare i dati al frontend per i grafici

logger = logging.getLogger(__name__)
stats_bp = Blueprint('statistics', __name__)

def calculate_content_stats(content_path: str):
    """
    Funzione helper che legge un file di testo e calcola le statistiche.
    Restituisce un dizionario con le metriche calcolate.
    """
    try:
        if not content_path or not os.path.exists(content_path):
            logger.warning(f"File non trovato o percorso nullo: {content_path}")
            return None
        
        with open(content_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.strip():
            return {'word_count': 0, 'gunning_fog': 0}

        # textstat richiede un minimo di parole per calcoli sensati
        word_count = len(content.split())
        if word_count < 100: # Limite minimo per un indice di leggibilità affidabile
             # Calcoliamo comunque il gunning fog, ma siamo consapevoli del limite
             gunning_fog_index = textstat.gunning_fog(content)
        else:
             gunning_fog_index = textstat.gunning_fog(content)
        
        return {
            'word_count': word_count,
            'gunning_fog': gunning_fog_index
        }

    except Exception as e:
        logger.error(f"Errore nel calcolare le statistiche per il file {content_path}: {e}")
        return None

def get_stats_for_source_type(cursor: sqlite3.Cursor, user_id: str, source_type: str):
    """
    Recupera i dati per un tipo di fonte (es. 'videos'), calcola le statistiche per ogni elemento
    e restituisce un riepilogo aggregato.
    """
    table_map = {
        'videos': 'videos',
        'documents': 'documents',
        'articles': 'articles',
        'pages': 'pages'
    }
    path_column_map = {
        'videos': 'transcript', # Per i video il testo è direttamente nel DB
        'documents': 'filepath',
        'articles': 'extracted_content_path',
        'pages': 'extracted_content_path'
    }
    
    table_name = table_map.get(source_type)
    path_column = path_column_map.get(source_type)

    if not table_name or not path_column:
        return {} # Tipo di fonte non valido

    # Query per recuperare il contenuto o il percorso del file
    query = f"SELECT {path_column} FROM {table_name} WHERE user_id = ? AND processing_status = 'completed'"
    cursor.execute(query, (user_id,))
    
    all_stats = []
    for row in cursor.fetchall():
        content_or_path = row[0]
        
        if source_type == 'videos': # Caso speciale per i video
            if content_or_path and content_or_path.strip():
                word_count = len(content_or_path.split())
                gunning_fog = textstat.gunning_fog(content_or_path) if word_count >= 100 else 0
                all_stats.append({'word_count': word_count, 'gunning_fog': gunning_fog})
        else: # Per documenti e articoli
            stats = calculate_content_stats(content_or_path)
            if stats:
                all_stats.append(stats)

    if not all_stats:
        return {
            'count': 0, 'avg_word_count': 0, 'avg_gunning_fog': 0,
            'word_count_distribution': []
        }

    # Calcoli aggregati
    total_word_count = sum(s['word_count'] for s in all_stats)
    total_gunning_fog = sum(s['gunning_fog'] for s in all_stats)
    item_count = len(all_stats)

    return {
        'count': item_count,
        'avg_word_count': round(total_word_count / item_count) if item_count > 0 else 0,
        'avg_gunning_fog': round(total_gunning_fog / item_count, 2) if item_count > 0 else 0,
        'word_count_distribution': [s['word_count'] for s in all_stats] # Per il grafico box plot
    }


@stats_bp.route('/statistics')
@login_required
def statistics_page():
    """
    Renderizza la pagina delle statistiche, recuperando e calcolando i dati
    per ogni tipo di fonte dell'utente corrente.
    """
    db_path = current_app.config.get('DATABASE_FILE')
    user_id = current_user.id
    final_stats = {}
    conn = None

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Cicliamo su ogni tipo di fonte che vogliamo analizzare
        source_types = ['videos', 'documents', 'articles', 'pages']
        for source in source_types:
            final_stats[source] = get_stats_for_source_type(cursor, user_id, source)
            # Convertiamo la lista per il grafico in un formato JSON sicuro per l'HTML
            final_stats[source]['word_count_distribution_json'] = json.dumps(final_stats[source].get('word_count_distribution', []))

    except sqlite3.Error as e:
        logger.error(f"Errore Database nella pagina statistiche per l'utente {user_id}: {e}")
        # In caso di errore, passiamo comunque un dizionario vuoto per evitare che il template si rompa
        final_stats = {
            'error': 'Si è verificato un errore nel caricamento delle statistiche.'
        }
    finally:
        if conn:
            conn.close()
            
    return render_template('statistics.html', stats_data=final_stats)