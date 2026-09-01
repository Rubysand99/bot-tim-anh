import threading
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Discord đang hoạt động trực tuyến 24/7!"

def run():
    # Render tự động cấp cổng thông qua biến môi trường PORT
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
  
