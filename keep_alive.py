import requests
import time
import threading

def keep_alive():
    while True:
        try:
            # O'z serveringizni URL bilan almashtiring
            response = requests.get("https://asataxi.fly.dev")
            print(f"Serverga so'rov yuborildi: {response.status_code}")
        except Exception as e:
            print(f"Xatolik: {e}")
        time.sleep(30)  # 30 soniyada bir so'rov

def start_keep_alive():
    thread = threading.Thread(target=keep_alive)
    thread.daemon = True
    thread.start()

if __name__ == "__main__":
    start_keep_alive()
    while True:
        time.sleep(60)
