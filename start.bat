@echo off
title CloudGuard Launcher
cd /d "%~dp0"
echo Starting CloudGuard (FastAPI Backend + Vite Frontend)...
npm run dev
pause
