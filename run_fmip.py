# FILE: financial-market-project/run_fmip.py
# NEW FILE. Place it in the project root, next to the src/, ui/ and data/ folders.
# Start everything with one command:  python run_fmip.py
# It creates the API key once, starts both services, and opens the dashboard.

import os
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(PROJECT_DIR, ".env")
FORECAST_DIR = os.path.join(PROJECT_DIR, "src")
CLASSIFIER_DIR = os.path.join(PROJECT_DIR, "pump_dump_classifier", "src")
CLASSIFIER_MODEL = os.path.join(PROJECT_DIR, "pump_dump_classifier", "outputs", "pump_dump_model.joblib")
FORECAST_PORT = 8000
CLASSIFIER_PORT = 8001
STARTUP_TIMEOUT_SECONDS = 90


# Reads the saved key from the .env file, or returns nothing if there is none yet.
def read_saved_key():
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE) as env_file:
        for line in env_file:
            if line.startswith("FMP_API_KEY="):
                return line.strip().split("=", 1)[1]
    return None


# Creates a random key the first time, so the user never has to invent or type one.
def get_or_create_key():
    saved_key = read_saved_key()
    if saved_key:
        return saved_key
    new_key = secrets.token_urlsafe(32)
    with open(ENV_FILE, "w") as env_file:
        env_file.write(f"FMP_API_KEY={new_key}\n")
    return new_key


# Trains the classifier models on the first run because the API cannot start without them.
def train_classifier_if_needed():
    if os.path.exists(CLASSIFIER_MODEL):
        return
    print("First run: training the classifier models...")
    subprocess.run([sys.executable, "main.py"], cwd=CLASSIFIER_DIR, check=True)


# Starts one uvicorn service in its own folder with the shared key in its environment.
def start_service(app_path, folder, port, api_key):
    service_env = {**os.environ, "FMP_API_KEY": api_key}
    command = [sys.executable, "-m", "uvicorn", app_path, "--host", "127.0.0.1", "--port", str(port)]
    return subprocess.Popen(command, cwd=folder, env=service_env)


# Polls the health URL until the service answers, so the browser never opens too early.
def wait_until_ready(health_url):
    deadline = time.time() + STARTUP_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            urllib.request.urlopen(health_url, timeout=2)
            return True
        except OSError:
            time.sleep(1)
    return False


# Keeps the launcher alive until Ctrl+C or until one of the services crashes.
def wait_for_exit(services):
    try:
        while all(service.poll() is None for service in services):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")


# Shuts both services down so no port stays busy after the launcher closes.
def stop_services(services):
    for service in services:
        service.terminate()


# Starts the classifier and forecast services, then opens the dashboard.
def main():
    api_key = get_or_create_key()
    train_classifier_if_needed()
    classifier = start_service("serve_classifier_api:app", CLASSIFIER_DIR, CLASSIFIER_PORT, api_key)
    forecast = start_service("serve_api:app", FORECAST_DIR, FORECAST_PORT, api_key)
    services = [classifier, forecast]
    if wait_until_ready(f"http://127.0.0.1:{FORECAST_PORT}/health"):
        webbrowser.open(f"http://127.0.0.1:{FORECAST_PORT}/ui")
        wait_for_exit(services)
    else:
        print("Forecast API did not start in time. Check the messages above.")
    stop_services(services)


if __name__ == "__main__":
    main()