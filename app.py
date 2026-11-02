import os
import uuid
import traceback
from flask import Flask, render_template, request, jsonify
from create_wind_map import create_wind_map

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
STATIC_FOLDER = 'static'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_FOLDER, exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルが選択されていません'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'ファイル名が空です'}), 400

    if not file.filename.lower().endswith('.fit'):
        return jsonify({'error': 'FITファイル（.fit）を選択してください'}), 400

    try:
        unique_id = str(uuid.uuid4())[:8]
        temp_fit_path = os.path.join(UPLOAD_FOLDER, f"temp_{unique_id}.fit")
        output_map_filename = f"map_{unique_id}.html"
        output_map_path = os.path.join(STATIC_FOLDER, output_map_filename)

        file.save(temp_fit_path)

        # マップ生成処理の実行
        create_wind_map(temp_fit_path, output_html=output_map_path)

        if os.path.exists(temp_fit_path):
            os.remove(temp_fit_path)

        return jsonify({
            'success': True,
            'map_url': f"/static/{output_map_filename}"
        })

    except Exception as e:
        # サーバーログに詳細なスタックトレースを出力
        print("=== ERROR IN /upload ===")
        traceback.print_exc()
        return jsonify({'error': f"マップ生成エラー: {str(e)}"}), 500

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'サーバー内部エラーが発生しました。RenderのLogsを確認してください。'}), 500