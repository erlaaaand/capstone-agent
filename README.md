# Market Intelligence Agent

Microservice untuk mengambil data harga durian premium dari Google Shopping
via SerpApi dan menyimpannya sebagai **JSON mentah** ke disk.

Tidak ada LLM. Tidak ada normalisasi. Data SerpApi disimpan apa adanya,
siap diproses lebih lanjut atau dimasukkan ke database.

---

## Struktur Output

Setiap run menghasilkan satu folder di `data/runs/`:

```
data/runs/
└── 20250607_193045_abc123/
    ├── run_summary.json          ← metadata run
    ├── D197_musang_king.json     ← raw SerpApi response
    ├── D13_golden_bun.json
    ├── D24_sultan.json
    └── D2_dato_nina.json
```

### Format file per varietas (`D197_musang_king.json`)

```json
{
  "variety_code": "D197",
  "variety_name": "Musang King / Raja Kunyit / Mao Shan Wang",
  "query_used":   "durian musang king utuh berkulit kg",
  "fetched_at":   "2025-06-07T12:30:45.123456+00:00",
  "success":      true,
  "error":        null,
  "item_count":   15,
  "raw": {
    "search_metadata": { "id": "...", "status": "Success", ... },
    "search_parameters": { "engine": "google_shopping", "q": "...", ... },
    "search_information": { "total_results": "1,230 results", ... },
    "shopping_results": [
      {
        "position": 1,
        "title": "Durian Musang King Utuh Segar 2kg",
        "price": "Rp800.000",
        "extracted_price": 800000.0,
        "rating": 4.8,
        "reviews": 142,
        "source": "Toko Durian Segar",
        "link": "https://...",
        "thumbnail": "https://...",
        "delivery": "Gratis ongkir"
      },
      ...
    ],
    "inline_shopping_results": [ ... ]
  }
}
```

---

## Setup

### 1. Clone dan buat virtual environment

```bash
git clone <repo>
cd market-intelligence
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Konfigurasi `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```env
API_KEY="key-acak-untuk-proteksi-endpoint"   # generate: python -c "import secrets; print(secrets.token_hex(32))"
SERPAPI_KEY="your-serpapi-api-key"           # dari https://serpapi.com/manage-api-key
```

### 3. Jalankan

```bash
python run.py
```

Service berjalan di `http://localhost:8000`.

---

## API Endpoints

Semua endpoint (kecuali `/health` dan `/api/v1/health`) memerlukan header:
```
X-API-Key: <nilai API_KEY dari .env>
```

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `GET`  | `/health` | Health check sederhana |
| `GET`  | `/docs`   | Swagger UI |
| `POST` | `/api/v1/fetch/trigger` | Picu fetch di background (async) |
| `POST` | `/api/v1/fetch/run`     | Jalankan fetch dan tunggu hasilnya (sync) |
| `GET`  | `/api/v1/runs`          | Daftar semua run |
| `GET`  | `/api/v1/runs/latest`   | Data run terbaru (full JSON) |
| `GET`  | `/api/v1/runs/{dir}`    | Data run spesifik |
| `GET`  | `/api/v1/runs/{dir}/{variety_code}` | Data satu varietas |
| `GET`  | `/api/v1/health`        | Status service + info run terakhir |
| `GET`  | `/api/v1/scheduler`     | Status scheduler cron |

### Contoh: trigger fetch dan ambil hasilnya

```bash
# 1. Trigger fetch (langsung return, proses di background)
curl -X POST http://localhost:8000/api/v1/fetch/trigger \
  -H "X-API-Key: your-api-key"

# 2. Tunggu ~30-60 detik, lalu ambil hasilnya
curl http://localhost:8000/api/v1/runs/latest \
  -H "X-API-Key: your-api-key"

# 3. Atau jalankan sync (tunggu sampai selesai)
curl -X POST http://localhost:8000/api/v1/fetch/run \
  -H "X-API-Key: your-api-key"
```

---

## Konfigurasi `.env`

| Variabel | Default | Keterangan |
|----------|---------|------------|
| `API_KEY` | `""` | Key untuk header `X-API-Key` (wajib diset) |
| `SERPAPI_KEY` | `""` | API key SerpApi (wajib diset) |
| `DATA_DIR` | `data/runs` | Direktori output JSON |
| `DATA_MAX_RUNS_KEPT` | `30` | Maksimum run yang disimpan (0 = tidak hapus) |
| `CRON_HOUR` | `19` | Jam run otomatis (0–23) |
| `CRON_MINUTE` | `30` | Menit run otomatis |
| `TIMEZONE` | `Asia/Jakarta` | Timezone scheduler |
| `SCHEDULER_DISABLED` | `false` | Set `true` untuk nonaktifkan run otomatis |
| `SERPAPI_TIMEOUT_SEC` | `30` | Timeout per request HTTP |
| `SERPAPI_MAX_RETRIES` | `2` | Maksimum retry per query |
| `SERPAPI_CONCURRENT_LIMIT` | `2` | Request paralel ke SerpApi |

---

## Menambah Varietas

Edit `agent/queries.py`, tambahkan `DurianQuery` baru ke `DURIAN_QUERIES`:

```python
DurianQuery(
    variety_code   = "D200",
    variety_name   = "Duri Hitam / Ochee",
    search_queries = [
        "durian duri hitam utuh berkulit kg",
        "durian ochee segar utuh harga",
    ],
    min_results = 3,
    num_results = 20,
),
```

---

## Integrasi Database

Data JSON yang tersimpan di `data/runs/` siap diproses:

```python
import json
from pathlib import Path

# Baca satu file varietas
with open("data/runs/20250607_193045_abc123/D197_musang_king.json") as f:
    data = json.load(f)

# Akses produk
for item in data["raw"]["shopping_results"]:
    print(item["title"], item.get("extracted_price"))
```
