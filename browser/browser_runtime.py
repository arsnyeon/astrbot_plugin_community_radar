"""探测系统已安装的浏览器。

只返回可执行文件路径，不复制、不修改用户日常浏览器的配置目录。
"""
import os
import shutil
import subprocess
from pathlib import Path


def find_browser(explicit='', windows=None):
    windows = os.name == 'nt' if windows is None else windows
    if explicit:
        return explicit if Path(explicit).is_file() else ''
    if windows:
        # Edge first (normally preinstalled), then Chrome. Check per-user installs too.
        for suffix in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
            for key in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
                base = os.environ.get(key)
                if base:
                    path = Path(base) / suffix
                    if path.is_file():
                        return str(path)
        try:
            import winreg
            for exe in ('msedge.exe', 'chrome.exe'):
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                        try:
                            with winreg.OpenKey(hive, 'SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\' + exe, 0, winreg.KEY_READ | view) as key:
                                path = str(winreg.QueryValue(key, None)).strip('"')
                                if Path(path).is_file():
                                    return path
                        except OSError:
                            pass
        except ImportError:
            pass
    names = ('msedge', 'chrome') if windows else ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'microsoft-edge')
    return next((p for name in names if (p := shutil.which(name))), '')


def process_options():
    if os.name != 'nt':
        return {'start_new_session': True}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {'startupinfo': info, 'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
