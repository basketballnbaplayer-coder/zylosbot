import threading
import subprocess

def run_bot():
    # Запускает твоего Twitch-бота
    subprocess.run(["python", "main.py"])

def run_web():
    # Запускает Flask-сайт
    subprocess.run(["python", "app.py"])

if __name__ == "__main__":
    # Запускаем бота в отдельном фоновом потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Запускаем веб-панель в основном потоке (она держит порт для Render)
    run_web()
