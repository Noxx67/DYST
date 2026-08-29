import sys
sys.path.insert(0,".")
from dyst import media, config as cfg
from dyst.overlay import OverlayWindow
from PySide6.QtWidgets import QApplication
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
app = QApplication(sys.argv[:1])
items=media.scan(cfg.DEFAULTS["media_folder"])
i=next(x for x in items if "woolly" in x.path and x.kind=="image")
settings = i.settings or {}
print("GLOBAL config.image_display_seconds:", cfg.DEFAULTS["image_display_seconds"])
print("GLOBAL config.fade_seconds:", cfg.DEFAULTS["fade_seconds"])
print("PER-FILE settings from sidecar:")
print("  image_display_seconds:", settings.get("image_display_seconds"))
print("  fade_seconds:", settings.get("fade_seconds"))
print("  mode:", settings.get("mode"))
print("  volume:", settings.get("volume"))
print("sidecar_audio:", i.sidecar_audio)
print()
print("What _spawn_overlay would use:")
from main import _spawn_overlay
print("  image_seconds would be:", settings.get("image_display_seconds", settings.get("duration", cfg.DEFAULTS["image_display_seconds"])))
print("  fade_seconds would be:", settings.get("fade_seconds", cfg.DEFAULTS["fade_seconds"]))