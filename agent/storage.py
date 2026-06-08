# agent/storage.py
#
# Simpan hasil fetch ke disk sebagai JSON.
#
# Struktur direktori:
#   data/runs/
#   ├── 20250607_193045_abc12/          ← satu folder per run
#   │   ├── run_summary.json            ← metadata run (status, durasi, dll.)
#   │   ├── D197_musang_king.json       ← raw response per varietas
#   │   ├── D13_golden_bun.json
#   │   ├── D24_sultan.json
#   │   └── D2_dato_nina.json
#   └── 20250608_193102_def34/
#       └── ...
#
# Format file per-varietas:
#   {
#     "variety_code": "D197",
#     "variety_name": "Musang King / ...",
#     "query_used":   "durian musang king utuh berkulit kg",
#     "fetched_at":   "2025-06-07T12:30:45.123456+00:00",
#     "success":      true,
#     "error":        null,
#     "item_count":   15,
#     "raw": {
#       "search_metadata": { ... },        ← metadata SerpApi
#       "search_parameters": { ... },      ← parameter query yang dikirim
#       "search_information": { ... },     ← info total hasil
#       "shopping_results": [ ... ],       ← DAFTAR PRODUK UTAMA
#       "inline_shopping_results": [ ... ] ← produk inline (jika ada)
#     }
#   }

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from core import config
from core.logger import get_logger

logger = get_logger("agent.storage")


def _run_dir_name(run_id: str) -> str:
    """Format: YYYYMMDD_HHMMSS_<6char-id>"""
    now = datetime.now(timezone.utc)
    short_id = run_id.replace("-", "")[:6]
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{short_id}"


def _safe_filename(variety_code: str, variety_name: str) -> str:
    """Buat nama file yang aman dari variety_code + variety_name."""
    safe_name = re.sub(r"[^\w]", "_", variety_name.split("/")[0].strip().lower())
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")
    return f"{variety_code}_{safe_name}.json"


class RunStorage:
    """
    Kelola penyimpanan hasil satu run ke disk.

    Buat instance dengan RunStorage.create() untuk mendapatkan
    direktori run yang sudah disiapkan.
    """

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = run_dir
        self.run_id  = run_id

    @classmethod
    def create(cls, run_id: str) -> "RunStorage":
        """Buat direktori run baru dan kembalikan instance RunStorage."""
        base = config.DATA_DIR
        base.mkdir(parents=True, exist_ok=True)

        dir_name = _run_dir_name(run_id)
        run_dir  = base / dir_name
        run_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[Storage] Run directory: {run_dir}")
        return cls(run_dir=run_dir, run_id=run_id)

    def save_variety(self, result: dict) -> Path:
        """
        Simpan satu hasil varietas ke file JSON.

        Args:
            result: Dict dari fetcher._fetch_variety()

        Returns:
            Path ke file yang disimpan.
        """
        filename = _safe_filename(
            result["variety_code"],
            result["variety_name"],
        )
        file_path = self.run_dir / filename

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        size_kb = file_path.stat().st_size / 1024
        logger.info(
            f"[Storage] Tersimpan: {filename} "
            f"({result['item_count']} item | {size_kb:.1f} KB)"
        )
        return file_path

    def save_summary(self, summary: dict) -> Path:
        """Simpan ringkasan run ke run_summary.json."""
        file_path = self.run_dir / "run_summary.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"[Storage] Summary tersimpan: {file_path}")
        return file_path

    def list_files(self) -> List[Path]:
        """Daftar semua file JSON dalam run directory ini."""
        return sorted(self.run_dir.glob("*.json"))


# ══════════════════════════════════════════════════════════════════════════════
# Manajemen semua runs
# ══════════════════════════════════════════════════════════════════════════════

def list_runs() -> List[dict]:
    """
    Daftar semua run yang tersimpan, diurutkan dari terbaru.

    Returns:
        List[dict] dengan field: run_dir, run_id, created_at, summary (jika ada)
    """
    base = config.DATA_DIR
    if not base.exists():
        return []

    runs = []
    for run_dir in sorted(base.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue

        summary_file = run_dir / "run_summary.json"
        summary = None
        if summary_file.exists():
            try:
                with open(summary_file, encoding="utf-8") as f:
                    summary = json.load(f)
            except Exception:
                pass

        runs.append({
            "dir_name":   run_dir.name,
            "run_dir":    str(run_dir),
            "summary":    summary,
        })

    return runs


def get_run(dir_name: str) -> Optional[dict]:
    """
    Baca seluruh data satu run berdasarkan nama direktori.

    Returns:
        Dict berisi summary + list hasil per varietas, atau None jika tidak ditemukan.
    """
    run_dir = config.DATA_DIR / dir_name
    if not run_dir.exists() or not run_dir.is_dir():
        return None

    result = {"dir_name": dir_name, "varieties": [], "summary": None}

    for json_file in sorted(run_dir.glob("*.json")):
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)

            if json_file.name == "run_summary.json":
                result["summary"] = data
            else:
                result["varieties"].append(data)
        except Exception as exc:
            logger.warning(f"[Storage] Gagal baca {json_file}: {exc}")

    return result


def get_latest_run() -> Optional[dict]:
    """Kembalikan data run terbaru, atau None jika belum ada."""
    runs = list_runs()
    if not runs:
        return None
    return get_run(runs[0]["dir_name"])


def cleanup_old_runs() -> int:
    """
    Hapus run lama jika melebihi DATA_MAX_RUNS_KEPT.
    Kembalikan jumlah run yang dihapus.
    """
    if config.MAX_RUNS_KEPT <= 0:
        return 0

    base = config.DATA_DIR
    if not base.exists():
        return 0

    run_dirs = sorted(
        [d for d in base.iterdir() if d.is_dir()],
        reverse=True,
    )

    to_delete = run_dirs[config.MAX_RUNS_KEPT:]
    deleted   = 0

    for old_dir in to_delete:
        try:
            import shutil
            shutil.rmtree(old_dir)
            logger.info(f"[Storage] Hapus run lama: {old_dir.name}")
            deleted += 1
        except Exception as exc:
            logger.warning(f"[Storage] Gagal hapus {old_dir}: {exc}")

    return deleted
