"""Resolve the local OCR executable without carrying Windows paths into Linux."""
import os
import shutil
from pathlib import Path

def resolve_tesseract(configured=''):
    value = os.path.expandvars(configured.strip().strip('\"').strip("'"))
    if value:
        command = shutil.which(value)
        if command:
            return command,'configured'
        if Path(value).is_file():
            return str(Path(value).resolve()),'configured'
    command = shutil.which('tesseract')
    if command:
        return command,'path'
    root = Path(__file__).resolve().parent.parent
    bundled = root / '.tools' / 'tesseract' / 'tesseract.exe'
    if os.name == 'nt' and bundled.is_file():
        return str(bundled),'bundled'
    if os.name == 'nt':
        for key in ('ProgramFiles','LOCALAPPDATA'):
            base = os.environ.get(key)
            if base:
                candidate = Path(base)/'Tesseract-OCR'/'tesseract.exe'
                if candidate.is_file():
                    return str(candidate),'windows_install'
    return None,'unavailable'
