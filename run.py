import threading
import subprocess

def run_bot():
    subprocess.run(["python", "main.py"])

def run_web():
    # Указываем путь внутри папки webapp
    subprocess.run(["python", "webapp/app.py"])

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    run_web()
