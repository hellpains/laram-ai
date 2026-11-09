import os
import sys
import tempfile
from pathlib import Path

# Изолированная среда до импорта приложения: временная папка данных, демо-провайдеры, без Битрикса
_tmp = tempfile.mkdtemp(prefix="callnotes-test-")
os.environ.update(
    DATA_DIR=_tmp, STT_PROVIDER="mock", LLM_PROVIDER="mock", BITRIX_WEBHOOK_URL="",
    APP_PASSWORD="", WEBHOOK_SECRET="test-secret", YANDEX_API_KEY="", YANDEX_FOLDER_ID="",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
