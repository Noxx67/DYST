@echo off
rem DYST (did you see that? 👀) - dev bootstrap: create venv, install deps, run app.
cd /d "%~dp0\.."

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python main.py %*