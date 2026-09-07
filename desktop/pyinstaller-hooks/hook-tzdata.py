from pathlib import Path

import tzdata


datas = [(str(Path(tzdata.__file__).resolve().parent / "zoneinfo"), "tzdata/zoneinfo")]
