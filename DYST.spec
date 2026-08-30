# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('dyst')

# ---------------------------------------------------------------------------
# Size pruning for the Qt6 stack (see PROGRESS.md "PyInstaller build" section).
#
# PyInstaller's Qt hook collects whole plugin directories (QtGui declares
# `imageformats`, `iconengines`, `platforminputcontexts`, ...) which drags in
# unused Qt modules and their dependency trees. This app never uses ANY of the
# pruned files:
#
#   - opengl32sw.dll (20 MB software OpenGL renderer) - only needed by Qt Quick
#     / OpenGL; we draw with QPainter (raster) only.
#   - libEGL/libGLESv2/d3dcompiler (ANGLE) - same reason (not present in this
#     PySide6 build, kept here defensively).
#   - Qt6Pdf.dll + imageformats/qpdf.dll - PDF, unused.
#   - Qt6Svg.dll + iconengines/qsvgicon.dll + imageformats/qsvg.dll - SVG, unused.
#   - Qt6VirtualKeyboard.dll + platforminputcontexts/qtvirtualkeyboardplugin.dll
#   - Qt6Quick.dll + Qt6Qml.dll + Qt6QmlMeta/Models/WorkerScript.dll - pulled in
#     ONLY by the virtual keyboard plugin; we have no QML/Quick.
#   - qtuiotouchplugin.dll (generic TUIO touch) - unused.
#   - All imageformats/iconengines plugins - every image in the app is decoded
#     by Pillow and handed to QImage as raw bytes (dyst/overlay.py), never via
#     Qt image plugins.
#   - PySide6/translations (124 .qm files, ~7 MB) - app has no Qt UI strings.
#
# All playback essentials are KEPT: qwindows/qoffscreen/qdirect2d platforms,
# multimedia/ffmpegmediaplugin + windowsmediaplugin, networkinformation,
# styles/qmodernwindowsstyle, tls/*, Qt6Core/Gui/Widgets/Multimedia/Network,
# the bundled ffmpeg codec DLLs (avcodec/avformat/avutil/swresample/swscale).
# ---------------------------------------------------------------------------
_PRUNE_BIN = (
    'opengl32sw.dll',
    'libEGL.dll',
    'libGLESv2.dll',
    'd3dcompiler_',
    'Qt6Pdf',
    'Qt6Svg',
    'Qt6VirtualKeyboard',
    'Qt6Quick',
    'Qt6Qml',
    'qpdf.dll',
    'qsvgicon.dll',
    'qsvg.dll',
    'qtvirtualkeyboardplugin.dll',
    'qtuiotouchplugin.dll',
    'plugins\\imageformats',
    'plugins\\iconengines',
    'plugins\\platforminputcontexts',
)


def _keep_bin(item):
    """Binaries entries are (dest_name, src_path, typecode)."""
    return not any(p in item[0] for p in _PRUNE_BIN)


def _keep_data(item):
    """Data entries are (src_path, dest_dir). Drop all Qt translations."""
    dest = item[1].lower().replace('\\', '/')
    return not dest.startswith('pyside6/translations')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Apply Qt pruning after analysis, before packaging.
a.binaries = [b for b in a.binaries if _keep_bin(b)]
a.datas = [d for d in a.datas if _keep_data(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DYST',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DYST',
)