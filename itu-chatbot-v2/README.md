# ITU Offline Chatbot — Ollama + Open WebUI

An offline campus chatbot assembled from off-the-shelf parts, with one small custom job.

```text
campus browser ─► host nginx :80/443 ─► Open WebUI 127.0.0.1:3000 ─► Ollama (internal network only)
                                         │  login, chat UI, RAG,         ├─ chat model   (qwen3.5:4b)  
                                         │  vector store, citations      └─ embeddings   (qwen3-embedding:0.6b)
nightly timer ─► ingest container ───────┘  crawl approved ITU pages → upload → knowledge collection
```

| Part | What it does | Custom code |
|---|---|---|
| `ollama` | Runs the chat model and the embedding model. No published port, no internet route. | none |
| `open-webui` | Accounts, chat UI, document parsing, chunking, vector search, citations, admin panel. | none |
| `ingest` | Crawls the approved URLs (robots.txt, allow-list, rate limit) plus an optional folder of approved files, and keeps one knowledge collection in sync: add new, swap changed, remove deleted. | `ingest/ingest.py` (~500 lines) |
| host nginx | Faces the LAN, so `ufw` still controls access. Docker-published ports would bypass `ufw`. | config only |

Each part is replaceable without touching the others. Ollama can move to a GPU server or vLLM by changing `OLLAMA_BASE_URL`. A different UI can call Open WebUI's API. The ingest job only speaks Open WebUI's REST API.

---

## 1. Install prerequisites (Ubuntu 24.04)

```bash
# Docker Engine + compose plugin: follow docs.docker.com/engine/install/ubuntu, then
sudo apt install -y nginx ufw
sudo mkdir -p /opt/itu-chatbot && sudo rsync -a ./ /opt/itu-chatbot/
cd /opt/itu-chatbot
cp .env.example .env && chmod 600 .env
sed -i "s/^WEBUI_SECRET_KEY=.*/WEBUI_SECRET_KEY=$(openssl rand -hex 32)/" .env
nano .env        # check CRAWL_SEEDS / CRAWL_ALLOWED_HOSTS; leave OWUI_API_KEY empty for now
mkdir -p approved-docs
```

## 2. Start the stack and pull models

```bash
docker compose up -d
scripts/pull-models.sh          # attaches ollama to a temporary network, pulls, detaches
docker compose ps
```

`qwen3-embedding:0.6b` is about 0.6 GB and multilingual, which covers English and Urdu. `qwen3.5:4b` is about 3.3–4 GB. Change `LLM_MODEL` freely. **Changing `EMBEDDING_MODEL` means re-ingesting everything** (see §9).

### Why qwen3.5:4b on this box

| Item | RAM |
|---|---|
| qwen3.5:4b (Q4_K_M GGUF + 4K context) | ~4–4.5 GB |
| qwen3-embedding:0.6b | ~1 GB |
| Open WebUI | ~1 GB |
| Ubuntu, Docker, nginx | ~1.5 GB |
| **Total** | **~7.5 GB of 16** |

RAM is not the limit; the 2 CPU cores are. A 9B model fits in 16 GB, but it would make each answer roughly twice as slow, and the 4B is already the slowest that stays usable on 2 cores. Keep Q4_K_M: CPU inference speed is bound by memory bandwidth, so Q8_0 costs about 1.6× the time for a small quality gain.

Pull the model through Ollama (`ollama pull qwen3.5:4b`, which `scripts/pull-models.sh` does). Ollama's package is a GGUF. Do **not** use third-party Qwen3.5 GGUF files with `ollama create`: those ship the vision part as a separate `mmproj` file and currently fail in Ollama. For an air-gapped server, pull on a connected machine and move it with `scripts/export-bundle.sh`; the model travels inside the `itu-chatbot-ollama` volume.

## 3. First login and admin settings

From the server (or over an SSH tunnel: `ssh -L 3000:127.0.0.1:3000 server`), open `http://127.0.0.1:3000`. **The first account created becomes the admin.** Create it immediately.

Then, in **Admin Panel → Settings**:

* **Documents.** Confirm Embedding Model Engine = *Ollama* and model = `qwen3-embedding:0.6b`. Enable **Hybrid Search** (combines BM25 keyword matching with dense vectors so searches for specific faculty names like "Dr. Tauseef" retrieve the exact document). Set **Top K** to 6. Environment variables like `RAG_*` and `CHUNK_*` seed these settings on first start only. After that, the admin panel values stored in the database take priority, so make later changes there.
* **General.** Enable API keys if your version gates them. Keep new-user signup off.
* **Models.** Hide the raw `qwen3.5` and `qwen3-embedding` models from users once step 5 is done.

## 4. Create the ingest account and run the first sync

1. Create a user `ingest-bot` with the **admin** role. Sign in as it, go to **Settings → Account → API keys**, create a key, and put it in `.env` as `OWUI_API_KEY`.
2. Run:

```bash
docker compose run --rm ingest --check        # URL, key, collection
docker compose run --rm ingest --dry-run      # crawl and list what would be added; changes nothing
docker compose run --rm ingest                # real sync; prints a JSON summary
```

The first sync on 2 CPU cores is slow, because every chunk is embedded on the CPU. Run it outside working hours. Later runs upload only pages whose content changed.

For files that are not on the website (prospectus PDFs, fee schedules), drop **approved public** PDF/DOCX/MD files into `approved-docs/`. The next sync picks them up and removes ones you delete.

## 5. Build the assistant users actually talk to

**Workspace → Knowledge.** Open *ITU Public Website* and set its visibility so all signed-in users can read it.

**Workspace → Models → New.** Set:

* Name **ITU Assistant**, ID `itu-assistant` (this matches `DEFAULT_MODELS` in `.env`)
* Base model: `qwen3.5:4b`
* Knowledge: attach *ITU Public Website*
* Advanced parameters:
  * **Think / reasoning: Off.** Qwen3.5 can "think" before answering. On 2 cores that adds minutes per answer and does not help document look-up.
  * Temperature 0.3, top_p 0.8, top_k 20.
  * Presence penalty 1.5, which Qwen recommends to stop quantised models repeating themselves.
  * Max tokens 512.
* System prompt:

```text
You are the official information assistant for Information Technology University (ITU) in Lahore, Pakistan (located at Arfa Software Technology Park, Ferozepur Road, Lahore).
CRITICAL RULES:
1. ITU stands ONLY for Information Technology University Lahore. It has NO relation to Geneva or Switzerland.
2. Answer ONLY from the ITU context provided to you. If the context does not contain the answer, say you could not find it on ITU's website and suggest contacting the relevant ITU office. Never invent dates, fees, deadlines, names, or addresses.
3. DOCUMENT RECENCY & RELEVANCE:
   - Always prioritize the most recent information (e.g., 2025/2026 policies over older years like 2021/2023). If multiple documents mention conflicting dates or policies, answer with the most recent one.
   - Never quote procurement tenders, RFPs, or contractor bids as university student policies.
   - If retrieved context is from an unrelated or sample test document that does not answer the question, clearly state the specific information is not found.
4. Keep answers short, clear, and cite the source page. Reply in the language of the question (English or Urdu).
```

Make it the default model and hide the base models (§3), so users cannot bypass the knowledge restriction.

## 6. Put nginx and the firewall in front

```bash
sudo sed 's/<SERVER_LAN_IP>/10.10.20.15/' deploy/nginx-itu-chatbot.conf | sudo tee /etc/nginx/sites-available/itu-chatbot >/dev/null
sudo ln -sf /etc/nginx/sites-available/itu-chatbot /etc/nginx/sites-enabled/itu-chatbot
sudo rm -f /etc/nginx/sites-enabled/default && sudo nginx -t && sudo systemctl reload nginx

sudo ufw default deny incoming
sudo ufw allow from <CAMPUS_CIDR> to any port 80 proto tcp
sudo ufw allow from <ADMIN_CIDR>  to any port 22 proto tcp
sudo ufw enable
```

Set `WEBUI_URL` in `.env` to the address users will type. Move to 443 once IT issues an internal certificate (template at the bottom of the nginx file).

## 7. Nightly sync

```bash
sudo install -m 0644 deploy/itu-ingest.service deploy/itu-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now itu-ingest.timer
journalctl -u itu-ingest.service -n 50 --no-pager      # after a run
```

Safety behaviour built into the job:

* It removes pages only after a healthy crawl: all seeds reachable, at most 25% fetch errors.
* It never removes more than `PRUNE_MAX_FRACTION` of documents in one run. If a site redesign legitimately moves many pages, run once with `--force-prune`.
* 404/410 pages are treated as deleted. Timeouts and 5xx errors are treated as errors, so a site outage does not empty the collection.
* A failed upload keeps the previous version live.
* Identical content under several URLs is stored once.
* Each web page is stored as Markdown with a `Source: <url>` line, so citations always lead back to the page.

## 8. Users

Signup is off. Create accounts in the admin panel, or connect ITU's directory via Open WebUI's LDAP/OIDC settings. This is what makes it safe to add **role-restricted** knowledge collections later: create a separate collection and give access only to a group. Do that only after IT reviews it. The public collection must stay public-only.

## 9. Move to an air-gapped server, back up, upgrade

```bash
scripts/export-bundle.sh            # images + model/data/state volumes + SHA256SUMS
# copy the bundle folder via an approved channel, then on the target:
scripts/import-bundle.sh /path/to/bundle-YYYY-MM-DD
cp /path/to/bundle-YYYY-MM-DD/docker-compose.yml . && cp <your .env> .  && docker compose up -d
```

The `open-webui` volume contains user accounts and chat history as well as the vectors, so **treat the bundle as sensitive**. The same export is your backup. Before upgrading, export, then pin `OPEN_WEBUI_TAG` and `OLLAMA_TAG` in `.env` to tested versions.

To change the embedding model:

1. Delete the collection in Workspace → Knowledge.
2. Run `docker compose down`.
3. Run `docker volume rm itu-chatbot-ingest-state`.
4. Set `EMBEDDING_MODEL`, and set the same model in Admin → Documents.
5. Run `docker compose up -d`, then `scripts/pull-models.sh`, then `docker compose run --rm ingest`.

## Capacity, honestly

On 2 cores, `OLLAMA_NUM_PARALLEL=1` means one answer at a time. Other requests queue (up to `OLLAMA_MAX_QUEUE`). Expect tens of seconds to minutes per answer. That supports a pilot of a handful of users, not a campus service. The next step that changes the picture is hardware, not software: 8+ cores with a 3B–7B model, or one 12–16 GB GPU. Point `OLLAMA_BASE_URL` at it and nothing else changes.

## Troubleshooting

| Symptom | Check |
|---|---|
| Upload or RAG fails with "No embedding model is loaded" | Admin → Documents engine must be *Ollama*; run `scripts/pull-models.sh` so `qwen3-embedding:0.6b` exists. |
| `ingest --check` gives 401/403 | Key wrong or API keys disabled in Admin → General; the account must be admin. |
| Answers ignore ITU content | User picked a base model. Hide base models; make ITU Assistant the default. |
| "refusing to remove N docs" | Many pages disappeared at once. Confirm on the site, then `--force-prune`. |
| Very slow or the host swaps | `docker stats`; use a smaller `LLM_MODEL`; lower `OLLAMA_CONTEXT_LENGTH` and `RAG_TOP_K`. |
| 502 from nginx | `docker compose ps`; `docker compose logs open-webui --tail 100`. |
| Scanned PDFs give nothing | They have no text layer. OCR them before placing them in `approved-docs/`. |
| Faculty/document in knowledge base not retrieved in chat | Ensure Hybrid Search is ON in Admin → Documents. If chunks are missing in Chroma, run `docker cp scripts/sync_missing_chunks.py itu-chatbot-open-webui-1:/tmp/` then `docker exec itu-chatbot-open-webui-1 python /tmp/sync_missing_chunks.py`. |

## Test

```bash
pip install requests beautifulsoup4 lxml
python tests/test_ingest.py
```

This runs the ingest job end to end against a fake website and a fake Open WebUI API. It covers robots.txt, the off-site and image filters, query-string stripping, duplicates, no-op re-runs, updates, pruning, and outage protection.
