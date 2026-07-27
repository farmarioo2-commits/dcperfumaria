import json
import sys
import traceback
from app.services.gmail_nfe_import import sync_once

if __name__ == "__main__":
    try:
        print(json.dumps(sync_once(), ensure_ascii=False), flush=True)
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
