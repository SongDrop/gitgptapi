import os
import json
import logging
import mysql.connector
import azure.functions as func
from typing import List, Dict

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

def get_mysql_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
        charset="utf8mb4",
        use_pure=True
    )

async def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info('Processing list_db request...')
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch main database info
        cursor.execute("""
            SELECT
                d.id, d.name, d.description, d.type, d.isPublic,
                d.lastUpdated, d.documentCount, d.usageCount, d.rating,
                d.owner_id,
                d.vector_search_enabled,
                d.vector_search_endpoint,
                d.vector_search_key,
                d.vector_search_index,
                d.vector_search_semantic_config,
                d.vector_search_embedding_deployment,
                d.vector_search_embedding_endpoint,
                d.vector_search_embedding_key,
                d.vector_search_storage_endpoint,
                d.vector_search_storage_access_key,
                d.vector_search_storage_connection_string
            FROM databases d
        """)
        databases_raw = cursor.fetchall()

        # Fetch owners info
        cursor.execute("SELECT id, name FROM users")
        users = {u['id']: u['name'] for u in cursor.fetchall()}

        # Fetch contributors per database
        cursor.execute("""
            SELECT dc.database_id, u.id as user_id, u.name
            FROM database_contributors dc
            JOIN users u ON dc.user_id = u.id
        """)
        contributors_raw = cursor.fetchall()

        # Group contributors by database_id
        contributors_map: Dict[str, List[Dict]] = {}
        for c in contributors_raw:
            contributors_map.setdefault(c['database_id'], []).append({
                "id": c['user_id'],
                "name": c['name']
            })

        # Fetch files per database
        cursor.execute("""
            SELECT database_id, name, size, type, uploadedAt
            FROM database_files
        """)
        files_raw = cursor.fetchall()
        files_map: Dict[str, List[Dict]] = {}
        for f in files_raw:
            files_map.setdefault(f['database_id'], []).append({
                "name": f['name'],
                "size": f['size'],
                "type": f['type'],
                "uploadedAt": f['uploadedAt'].isoformat() if f['uploadedAt'] else None
            })

        # Batch fetch all tags once
        cursor = conn.cursor()
        cursor.execute("SELECT database_id, tag FROM database_tags")
        tags_raw = cursor.fetchall()
        cursor.close()

        tags_map: Dict[str, List[str]] = {}
        for db_id, tag in tags_raw:
            tags_map.setdefault(db_id, []).append(tag)

        cursor.close()
        conn.close()

        # Construct final list with nested objects
        databases = []
        for db in databases_raw:
            db_id = db['id']
            owner_id = db['owner_id']
            databases.append({
                "id": db_id,
                "name": db['name'],
                "description": db['description'],
                "type": db['type'],
                "owner": {
                    "id": owner_id,
                    "name": users.get(owner_id, "Unknown")
                },
                "contributors": contributors_map.get(db_id, []),
                "isPublic": bool(db['isPublic']),
                "tags": tags_map.get(db_id, []),
                "lastUpdated": db['lastUpdated'].isoformat() if db['lastUpdated'] else None,
                "documentCount": db['documentCount'],
                "usageCount": db['usageCount'],
                "rating": float(db['rating']) if db['rating'] is not None else None,
                "isSelected": False,
                "files": files_map.get(db_id, []),

                # Vector Search Configuration fields read from database columns
                "VECTOR_SEARCH_ENABLED": bool(db.get('vector_search_enabled', False)),
                "VECTOR_SEARCH_ENDPOINT": db.get('vector_search_endpoint'),
                "VECTOR_SEARCH_KEY": db.get('vector_search_key'),
                "VECTOR_SEARCH_INDEX": db.get('vector_search_index'),
                "VECTOR_SEARCH_SEMANTIC_CONFIG": db.get('vector_search_semantic_config'),
                "VECTOR_SEARCH_EMBEDDING_DEPLOYMENT": db.get('vector_search_embedding_deployment'),
                "VECTOR_SEARCH_EMBEDDING_ENDPOINT": db.get('vector_search_embedding_endpoint'),
                "VECTOR_SEARCH_EMBEDDING_KEY": db.get('vector_search_embedding_key'),
                "VECTOR_SEARCH_STORAGE_ENDPOINT": db.get('vector_search_storage_endpoint'),
                "VECTOR_SEARCH_STORAGE_ACCESS_KEY": db.get('vector_search_storage_access_key'),
                "VECTOR_SEARCH_STORAGE_CONNECTION_STRING": db.get('vector_search_storage_connection_string'),
            })

        # Return JSON response with all databases
        response_payload = {
            "databases": databases,
        }

        return func.HttpResponse(
            json.dumps(response_payload, indent=2),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        logger.error(f"Error in main function: {e}", exc_info=True)
        return func.HttpResponse(f"Error: {e}", status_code=500)