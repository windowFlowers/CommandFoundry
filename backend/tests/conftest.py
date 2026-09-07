from __future__ import annotations

import os
import tempfile
from pathlib import Path


os.environ["AEGIS_EMBEDDING_ENABLED"] = "0"
os.environ["AEGIS_LLM_API_KEY"] = ""
os.environ["AEGIS_STORAGE_DIR"] = str(Path(tempfile.mkdtemp(prefix="aegis-tests-")))
