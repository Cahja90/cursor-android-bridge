#chats je fenster, drei tage, bleibt über den neustart
import json
import time
from pathlib import Path

CHAT_KEEP_SEC = 3 * 24 * 60 * 60
CHAT_CHECK_SEC = 24 * 60 * 60


def prune_windows(windows, now, keep_sec, keep_hwnd=""):
    #älter als drei tage weg, der offene verlauf des aktuellen fensters bleibt
    grenze = float(now) - float(keep_sec)
    keep = str(keep_hwnd or "")
    neu = {}
    for key, row in (windows or {}).items():
        if not isinstance(row, dict):
            continue
        items = []
        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            at = item.get("at")
            if not isinstance(at, (int, float)):
                items.append(item)
                continue
            if float(at) >= grenze:
                items.append(item)
                continue
            if str(key) == keep and item.get("role") == "agent" and not item.get("closed"):
                items.append(item)
        if not items and str(key) != keep:
            continue
        kopie = dict(row)
        kopie["items"] = items
        neu[str(key)] = kopie
    return neu


class ChatBook:
    def __init__(self, path):
        self.path = Path(path)
        self.windows = {}
        self.load()

    def load(self):
        if not self.path.is_file():
            self.windows = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.windows = {}
            return
        windows = data.get("windows") if isinstance(data, dict) else None
        self.windows = windows if isinstance(windows, dict) else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"windows": self.windows}, ensure_ascii=False)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(raw, encoding="utf-8")
        tmp.replace(self.path)

    def bucket(self, hwnd, title="", process=""):
        key = str(int(hwnd or 0))
        row = self.windows.get(key)
        if not isinstance(row, dict):
            row = {
                "title": "",
                "process": "",
                "updated": time.time(),
                "thought": "",
                "items": [],
            }
            self.windows[key] = row
        if title:
            row["title"] = title
        if process:
            row["process"] = process
        if not isinstance(row.get("items"), list):
            row["items"] = []
        if not isinstance(row.get("thought"), str):
            row["thought"] = ""
        return row

    def items(self, hwnd):
        key = str(int(hwnd or 0))
        row = self.windows.get(key)
        if not isinstance(row, dict):
            return []
        return list(row.get("items") or [])

    def thought(self, hwnd):
        key = str(int(hwnd or 0))
        row = self.windows.get(key)
        if not isinstance(row, dict):
            return ""
        return str(row.get("thought") or "")

    def set_thought(self, hwnd, text):
        row = self.bucket(hwnd)
        row["thought"] = (text or "")[-240:]
        row["updated"] = time.time()

    def append_item(self, hwnd, item, limit):
        row = self.bucket(hwnd)
        eintrag = dict(item)
        if not isinstance(eintrag.get("at"), (int, float)):
            eintrag["at"] = time.time()
        row["items"].append(eintrag)
        row["updated"] = eintrag["at"]
        if len(row["items"]) > limit:
            del row["items"][: len(row["items"]) - limit]
        return eintrag

    def prune(self, now, keep_hwnd):
        self.windows = prune_windows(self.windows, now, CHAT_KEEP_SEC, keep_hwnd)
