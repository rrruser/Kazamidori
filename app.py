import os
import uuid
from flask import Flask, request, render_template, jsonify

from create_wind_map import create_wind_map

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB上限

# 一時保存フォルダおよびマップ出力先の確保
UPLOAD_FOLDER = '/tmp' if os.path.exists('/tmp') else 'temp'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('static', exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルが選択されていません。'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'ファイルが選択されていません。'}), 400

    # 元のファイル拡張子を取得 (.fit / .tcx 対応)
    ext = os.path.splitext(file.filename)[1].lower()
    if not ext:
        ext = '.fit'

    # リクエストごとに固有のUUIDを生成してファイル名の衝突・ロックを防止
    unique_id = uuid.uuid4().hex
    temp_file_path = os.path.join(UPLOAD_FOLDER, f"{unique_id}{ext}")
    output_map_filename = f"map_{unique_id}.html"
    output_map_path = os.path.join('static', output_map_filename)

    try:
        # 1. 一時アクティビティファイルの保存
        file.save(temp_file_path)

        # 2. マップHTMLの生成
        create_wind_map(temp_file_path, output_html=output_map_path)

        # 3. 生成されたHTMLのURLを返却
        return jsonify({
            'success': True,
            'map_url': f"/static/{output_map_filename}"
        })

    except Exception as e:
        print(f"[Upload Error] {e}")
        return jsonify({'error': f"マップ生成エラー: {str(e)}"}), 500

    finally:
        # 使用済みの一時ファイルを確実に削除してストレージ開放＆ファイルロック回避
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'ファイルサイズが大きすぎます (上限32MB)。'}), 413

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'サーバー内部エラーが発生しました。RenderのLogsを確認してください。'}), 500

if __name__ == '__main__':
    app.run(debug=True)