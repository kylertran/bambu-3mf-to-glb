@echo off
rem Double-click to open the Bambu 3MF to GLB converter GUI.
rem You can also drop a .3mf file onto this .bat to open it pre-loaded.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw bambu3mf_gui.py %* & exit /b)
where pyw >nul 2>nul && (start "" pyw bambu3mf_gui.py %* & exit /b)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" bambu3mf_gui.py %* & exit /b)
echo Python was not found. Install Python 3 and run: pip install -r requirements.txt
pause
