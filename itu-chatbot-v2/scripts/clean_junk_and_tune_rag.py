"""
Purge junk procurement, tender, and RFP files (and their vector chunks)
from Open WebUI database and Chroma collection.
Also tune relevance threshold and BM25 weight to prevent false retrievals.
"""
import sqlite3
import chromadb

DB_PATH = "/app/backend/data/webui.db"
VECTOR_PATH = "/app/backend/data/vector_db"

JUNK_KEYWORDS = [
    "rfp", "proc", "tender", "auction", "budget-estimates", 
    "peeda", "consultan", "procurement"
]

def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # 1. Tune RAG parameters using key-value pairs
    cur.execute("UPDATE config SET value='0.0' WHERE key='rag.relevance_threshold';")
    cur.execute("UPDATE config SET value='0.5' WHERE key='rag.hybrid_bm25_weight';")
    cur.execute("UPDATE config SET value='10' WHERE key='rag.top_k';")
    con.commit()
    print("Updated RAG parameters: relevance_threshold=0.0, hybrid_bm25_weight=0.5, top_k=10")

    # 2. Identify target collection
    cur.execute("SELECT id, name FROM knowledge WHERE name LIKE '%ITU%';")
    k_row = cur.fetchone()
    if not k_row:
        print("Knowledge base not found.")
        return
    kid, kname = k_row
    print(f"Target Knowledge Base: {kname} ({kid})")

    # 3. Find junk files
    cur.execute("SELECT id, filename FROM file;")
    files = cur.fetchall()
    
    junk_files = []
    for fid, fname in files:
        fl = fname.lower()
        if any(kw in fl for kw in JUNK_KEYWORDS):
            junk_files.append((fid, fname))

    print(f"\nFound {len(junk_files)} junk procurement/tender files out of {len(files)} total files.")

    # 4. Connect to Chroma and delete chunks
    client = chromadb.PersistentClient(path=VECTOR_PATH)
    know_col = client.get_collection(kid)
    initial_chunks = know_col.count()
    print(f"Initial chunks in collection: {initial_chunks}")

    total_chunks_deleted = 0
    for fid, fname in junk_files:
        existing = know_col.get(where={"file_id": fid})
        chunk_ids = existing.get("ids", [])
        if chunk_ids:
            know_col.delete(ids=chunk_ids)
            total_chunks_deleted += len(chunk_ids)
        
        try:
            client.delete_collection(f"file-{fid}")
        except Exception:
            pass

        cur.execute("DELETE FROM knowledge_file WHERE file_id = ?;", (fid,))
        cur.execute("DELETE FROM file WHERE id = ?;", (fid,))

    con.commit()

    print(f"Deleted {total_chunks_deleted} junk chunks.")
    print(f"Remaining chunks in collection: {know_col.count()} (clean academic & campus content).")

if __name__ == "__main__":
    main()

