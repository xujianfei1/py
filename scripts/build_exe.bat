@echo off
rem 一键打包视频播放器: 需要 Python 3.10+ 和 pip install PySide6 pyinstaller
pyinstaller --onefile --noconsole --name video_player video_player.py
echo.
echo 产物在 dist\video_player.exe
