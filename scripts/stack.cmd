@echo off
rem Windows entry point for scripts/stack.sh, so the stack can be driven
rem without an open Git Bash:
rem
rem   scripts\stack.cmd status
rem   scripts\stack.cmd start
rem   scripts\stack.cmd stop
rem   scripts\stack.cmd restart
rem
rem For a double-click, use scripts\stack-start.cmd / scripts\stack-stop.cmd.
setlocal
cd /d "%~dp0.."

rem Git Bash only. The `bash.exe` that Windows ships in WindowsApps is the WSL
rem launcher, where cygpath does not exist and every helper here would break.
set "STACK_BASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "STACK_BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined STACK_BASH if exist "%ProgramFiles%\Git\usr\bin\bash.exe" set "STACK_BASH=%ProgramFiles%\Git\usr\bin\bash.exe"
if not defined STACK_BASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "STACK_BASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined STACK_BASH (
  echo [FAIL] Git Bash not found. Install Git for Windows, or run from Git Bash: bash scripts/stack.sh %*
  exit /b 1
)

"%STACK_BASH%" scripts/stack.sh %*
exit /b %ERRORLEVEL%
