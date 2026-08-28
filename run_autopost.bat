@echo off
cd /d "D:\humshehry facebook page"
"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe" main.py --once --cron --max-posts 1 --no-delay >> "logs\autopost_task.log" 2>&1
