@echo off
rem Single source of truth for the base Python environment of this project.
rem Edit PYTHON_DIR below only; app.bat / admin.bat / mcp_server.bat call this
rem file, and child processes (instances, run_script, skill scripts) inherit
rem it via sys.executable or the LOCOHANE_PYTHON environment variable.
set PYTHON_DIR=C:\DT_Python\Python311\env_local_agent_system
set LOCOHANE_PYTHON=%PYTHON_DIR%\Scripts\python.exe
set PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%
rem Python UTF-8 mode: stdio and open() default to UTF-8 regardless of the
rem Windows language/code page (e.g. English Windows with cp1252/cp437), so
rem Japanese paths, file contents and logs never raise UnicodeEncodeError.
set PYTHONUTF8=1
