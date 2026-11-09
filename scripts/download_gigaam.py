"""Скачивает веса GigaAM при сборке Docker-образа, чтобы первый звонок не ждал загрузки.

Запуск: python scripts/download_gigaam.py v3_e2e_rnnt
"""
import sys

import gigaam

name = sys.argv[1] if len(sys.argv) > 1 else "v3_e2e_rnnt"
try:
    gigaam.load_model(name, device="cpu")
except TypeError:
    gigaam.load_model(name)
print(f"GigaAM {name}: модель загружена")
