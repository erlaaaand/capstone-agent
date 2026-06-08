"""
run.py — Entry point.

Jalankan dengan: python run.py
Jangan jalankan uvicorn langsung karena ProactorEventLoop harus diset
sebelum uvicorn membuat event loop-nya.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    print("[run.py] WindowsProactorEventLoopPolicy aktif.")

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host    = "0.0.0.0",
        port    = 8001,
        reload  = False,
        loop    = "asyncio",
    )
