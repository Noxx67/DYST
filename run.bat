@echo off
rem DYST (did you see that? 👀) - run with the project virtualenv.
rem Use this instead of plain "python main.py" so PySide6/opencv are found.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python main.py %*