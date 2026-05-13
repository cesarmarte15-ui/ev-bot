import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from ev_engine import SPORTS, get_dashboard, debug_all_sports, clear_cache

load_dotenv()
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html', sports=SPORTS)

@app.route('/api/dashboard')
def api_dashboard():
    selected_sports = request.args.getlist('sports') or ['MLB', 'NBA', 'NHL']
    force_refresh = request.args.get('refresh', '0') == '1'
    return jsonify(get_dashboard(selected_sports, force_refresh=force_refresh))

@app.route('/api/debug')
def api_debug():
    return jsonify(debug_all_sports())

@app.route('/api/clear-cache')
def api_clear_cache():
    clear_cache()
    return jsonify({'ok': True, 'message': 'Cache limpiado'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
