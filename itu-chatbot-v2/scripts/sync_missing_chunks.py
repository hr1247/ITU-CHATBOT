"""
Sync any files registered in Open WebUI's Knowledge Base whose chunks
are present in their individual file collection (file-{file_id})
but missing from the main knowledge vector collection.
Also updates RAG settings for hybrid search and top_k.
"""
import sqlite3
import sys
import chromadb

DB_PATH = "/app/backend/data/webui.db"
VECTOR_PATH = "/app/backend/data/vector_db"

def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # 1. Update RAG settings in webui.db
    cur.execute("UPDATE config SET value='true' WHERE key='rag.enable_hybrid_search';")
    cur.execute("UPDATE config SET value='true' WHERE key='rag.enable_hybrid_search_enriched_texts';")
    cur.execute("UPDATE config SET value='6' WHERE key='rag.top_k';")
    con.commit()
    print("Updated webui.db RAG settings: hybrid_search=true, enriched_texts=true, top_k=6")

    # 2. Find ITU Public Website collection ID
    cur.execute("SELECT id, name FROM knowledge WHERE name LIKE '%ITU%';")
    knowledge_rows = cur.fetchall()
    if not knowledge_rows:
        print("No ITU knowledge collection found.")
        return
    
    kid, kname = knowledge_rows[0]
    print(f"Target knowledge base: {kname} (ID: {kid})")

    # 3. Connect to Chroma
    client = chromadb.PersistentClient(path=VECTOR_PATH)
    try:
        know_col = client.get_collection(kid)
    except Exception as e:
        print(f"Failed to get collection {kid}: {e}")
        return

    # 4. Get all files attached to this knowledge base
    cur.execute("""
        SELECT kf.file_id, f.filename 
        FROM knowledge_file kf
        LEFT JOIN file f ON kf.file_id = f.id
        WHERE kf.knowledge_id = ?
    """, (kid,))
    files = cur.fetchall()
    print(f"Total files associated with {kname}: {len(files)}")

    missing_synced = 0
    total_chunks_added = 0

    for file_id, filename in files:
        if not file_id:
            continue
        
        # Check existing chunks in knowledge collection
        existing = know_col.get(where={"file_id": file_id})
        count_in_know = len(existing["ids"]) if existing and "ids" in existing else 0

        if count_in_know == 0:
            file_col_name = f"file-{file_id}"
            try:
                f_col = client.get_collection(file_col_name)
            except Exception:
                continue

            f_data = f_col.get(include=["documents", "embeddings", "metadatas"])
            f_ids = f_data.get("ids", [])
            f_docs = f_data.get("documents", [])
            f_embs = f_data.get("embeddings", [])
            f_metas = f_data.get("metadatas", [])

            if f_ids:
                # Update collection_name in metadata
                updated_metas = []
                for m in f_metas:
                    meta_copy = dict(m) if m else {}
                    meta_copy["collection_name"] = kid
                    meta_copy["file_id"] = file_id
                    if filename and "name" not in meta_copy:
                        meta_copy["name"] = filename
                    updated_metas.append(meta_copy)

                # Upsert into knowledge collection
                know_col.upsert(
                    ids=f_ids,
                    documents=f_docs,
                    embeddings=f_embs,
                    metadatas=updated_metas
                )
                print(f"Synced missing file into collection: {filename or file_id} ({len(f_ids)} chunks)")
                missing_synced += 1
                total_chunks_added += len(f_ids)

    print(f"\nDone! Synced {missing_synced} missing files ({total_chunks_added} chunks added).")
    print(f"Total chunks in knowledge collection now: {know_col.count()}")

if __name__ == "__main__":
    main()

