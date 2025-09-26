import logging
import sqlite3
import json
import os
import textstat
from flask import Blueprint, render_template, current_app, jsonify, request, flash, redirect, url_for
from flask_login import login_required, current_user

logger = logging.getLogger(__name__)
stats_bp = Blueprint('statistics', __name__)

@stats_bp.route('/statistics')
@login_required
def statistics_page():
    db_path = current_app.config.get('DATABASE_FILE')
    user_id = current_user.id
    final_stats = {}
    conn = None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        source_types = ['videos', 'documents', 'articles', 'pages']
        for source in source_types:
            query = """
                SELECT
                    COUNT(content_id) as item_count,
                    AVG(word_count) as avg_word_count,
                    AVG(gunning_fog) as avg_gunning_fog,
                    json_group_array(word_count) as word_count_dist
                FROM content_stats
                WHERE user_id = ? AND source_type = ?
            """
            cursor.execute(query, (user_id, source))
            data = cursor.fetchone()

            if data and data['item_count'] > 0:
                final_stats[source] = {
                    'count': data['item_count'],
                    'avg_word_count': round(data['avg_word_count'] or 0),
                    'avg_gunning_fog': round(data['avg_gunning_fog'] or 0, 2),
                    'word_count_distribution_json': data['word_count_dist'] or '[]'
                }
            else:
                final_stats[source] = {'count': 0, 'word_count_distribution_json': '[]'}

        query_stats = {
            'total_queries': 0,
            'source_counts': {},
            'recent_queries': [],
            'daily_query_trend': []
        }
        
        cursor.execute("SELECT source, COUNT(*) as count FROM query_logs GROUP BY source")
        source_data = cursor.fetchall()
        for row in source_data:
            query_stats['source_counts'][row['source']] = row['count']
            query_stats['total_queries'] += row['count']

        cursor.execute("SELECT query_text, source, created_at FROM query_logs ORDER BY created_at DESC LIMIT 10")
        query_stats['recent_queries'] = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT 
                DATE(created_at) as query_date, 
                COUNT(*) as query_count
            FROM query_logs
            WHERE created_at >= DATE('now', '-30 days')
            GROUP BY query_date
            ORDER BY query_date ASC
        """)
        daily_trend_data = [dict(row) for row in cursor.fetchall()]
        query_stats['daily_query_trend'] = daily_trend_data

        final_stats['query_logs'] = query_stats

    except sqlite3.Error as e:
        logger.error(f"Errore Database nella pagina statistiche per l'utente {user_id}: {e}")
        final_stats = {'error': 'Si è verificato un errore nel caricamento delle statistiche.'}
    finally:
        if conn:
            conn.close()
            
    return render_template('statistics.html', stats_data=final_stats)

@stats_bp.route('/statistics/recalculate', methods=['POST'])
@login_required
def recalculate_stats():
    """
    Funzione per ricalcolare e salvare le statistiche per TUTTI i contenuti
    esistenti di un utente, leggendo il testo direttamente dal database.
    """
    db_path = current_app.config.get('DATABASE_FILE')
    user_id = current_user.id
    conn = None
    processed_counts = {}

    logger.info(f"Avvio ricalcolo statistiche per l'utente {user_id}...")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Configurazione delle fonti aggiornata per leggere sempre dal DB
        source_config = {
            'videos': {'table': 'videos', 'id_col': 'video_id', 'content_col': 'transcript'},
            'documents': {'table': 'documents', 'id_col': 'doc_id', 'content_col': 'content'},
            'articles': {'table': 'articles', 'id_col': 'article_id', 'content_col': 'content'},
            'pages': {'table': 'pages', 'id_col': 'page_id', 'content_col': 'content'}
        }

        for source_type, config in source_config.items():
            processed_counts[source_type] = 0
            logger.info(f"Ricalcolo per: {source_type}...")
            
            query = f"SELECT {config['id_col']}, {config['content_col']} FROM {config['table']} WHERE user_id = ? AND processing_status = 'completed'"
            cursor.execute(query, (user_id,))
            
            items_to_process = cursor.fetchall()
            
            for item in items_to_process:
                content_id, content_text_from_db = item[0], item[1]
                
                # La logica ora è unificata e semplice
                content_text = content_text_from_db or ''

                if content_text.strip():
                    word_count = len(content_text.split())
                    gunning_fog = textstat.gunning_fog(content_text)
                    
                    cursor.execute("""
                        INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(content_id) DO UPDATE SET
                            word_count = excluded.word_count,
                            gunning_fog = excluded.gunning_fog,
                            last_calculated = CURRENT_TIMESTAMP
                    """, (content_id, user_id, source_type, word_count, gunning_fog))
                    
                    processed_counts[source_type] += 1

        conn.commit()
        logger.info(f"Ricalcolo completato per l'utente {user_id}. Dettagli: {processed_counts}")
        flash('Le statistiche per tutti i contenuti esistenti sono state ricalcolate con successo!', 'success')

    except sqlite3.Error as e:
        logger.error(f"Errore DB durante il ricalcolo per l'utente {user_id}: {e}")
        if conn: conn.rollback()
        flash('Si è verificato un errore durante il ricalcolo delle statistiche.', 'error')
    finally:
        if conn: conn.close()

    return redirect(url_for('statistics.statistics_page'))