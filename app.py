import os
import uuid
from flask import Flask, render_template, request, jsonify
from create_wind_map import create_wind_map

app = Flask(__name__)

# アップロード先および生成用フォルダの準備
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
        # キャッシュバスター用に一意のIDを生成
        unique_id = str(uuid.uuid4())[:8]
        temp_fit_path = os.path.join(UPLOAD_FOLDER, f"temp_{unique_id}.fit")
        output_map_filename = f"map_{unique_id}.html"
        output_map_path = os.path.join(STATIC_FOLDER, output_map_filename)

        # ファイル保存
        file.save(temp_fit_path)

        # 既存のマップ生成モジュールを実行
        create_wind_map(temp_fit_path, output_html=output_map_path)

        # 一時FITファイルの削除
        if os.path.exists(temp_fit_path):
            os.remove(temp_fit_path)

        # フロントエンドに生成されたマップのURLを返却
        return jsonify({
            'success': True,
            'map_url': f"/static/{output_map_filename}"
        })

    except Exception as e:
        return jsonify({'error': f"処理中にエラーが発生しました: {str(e)}"}), 500

if __name__ == '__main__':
    # ローカル開発サーバー起動 (http://127.0.0.1:5000)
    app.run(debug=False, port=5000)