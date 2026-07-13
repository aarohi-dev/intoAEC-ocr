import os
import json
import csv
import tempfile
import io
from flask import Flask, request, render_template, redirect, url_for, flash, Response
from werkzeug.utils import secure_filename
from parser import extract_data

app = Flask(__name__)
app.secret_key = 'super_secret_ocr_key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max

# We'll store the latest extraction in memory for demo purposes.
# In a real app, this would be in a database or session.
latest_data = None

@app.route('/', methods=['GET'])
def app_workspace():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    global latest_data
    if 'pdf_file' not in request.files:
        flash('No file part')
        return redirect(request.url)
    
    file = request.files['pdf_file']
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('app_workspace'))
        
    if file and file.filename.endswith('.pdf'):
        fd, temp_path = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        try:
            file.save(temp_path)
            # Extract data
            latest_data = extract_data(temp_path)
            os.remove(temp_path)
            return render_template('result.html', data=latest_data)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return f"An error occurred during extraction: {str(e)}", 500
    
    flash('Invalid file format. Please upload a PDF.')
    return redirect(url_for('app_workspace'))

@app.route('/download/json', methods=['GET'])
def download_json():
    if not latest_data:
        return "No data available", 404
    return Response(
        json.dumps(latest_data, indent=2),
        mimetype="application/json",
        headers={"Content-disposition": "attachment; filename=extracted_full_data.json"}
    )

@app.route('/download/csv', methods=['GET'])
def download_csv():
    if not latest_data or not latest_data.get('tables'):
        return "No tables available", 404
        
    output = io.StringIO()
    writer = csv.writer(output)
    
    if latest_data.get('summary_table'):
        writer.writerow(["--- SUMMARIZED REPORT ---"])
        for row in latest_data['summary_table']:
            writer.writerow(row)
        writer.writerow([])
        writer.writerow(["--- RAW EXTRACTED TABLES ---"])
        
    for i, table in enumerate(latest_data['tables']):
        writer.writerow([f"--- Table {i+1} ---"])
        for row in table:
            writer.writerow(row)
        writer.writerow([])
        
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=extracted_tables.csv"}
    )

@app.route('/download/summary/csv', methods=['GET'])
def download_summary_csv():
    if not latest_data or not latest_data.get('summary_table'):
        return "No summary table available", 404
        
    output = io.StringIO()
    writer = csv.writer(output)
    for row in latest_data['summary_table']:
        writer.writerow(row)
        
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=summarized_report.csv"}
    )

@app.route('/download/summary/json', methods=['GET'])
def download_summary_json():
    if not latest_data or not latest_data.get('summary_table'):
        return "No summary table available", 404
        
    return Response(
        json.dumps(latest_data['summary_table'], indent=2),
        mimetype="application/json",
        headers={"Content-disposition": "attachment; filename=summarized_report.json"}
    )

@app.route('/download/raw_tables/csv', methods=['GET'])
def download_raw_tables_csv():
    if not latest_data or not latest_data.get('tables'):
        return "No raw tables available", 404
        
    output = io.StringIO()
    writer = csv.writer(output)
    for i, table in enumerate(latest_data['tables']):
        writer.writerow([f"--- Table {i+1} ---"])
        for row in table:
            writer.writerow(row)
        writer.writerow([])
        
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=raw_tables.csv"}
    )

@app.route('/download/raw_tables/json', methods=['GET'])
def download_raw_tables_json():
    if not latest_data or not latest_data.get('tables'):
        return "No raw tables available", 404
        
    return Response(
        json.dumps(latest_data['tables'], indent=2),
        mimetype="application/json",
        headers={"Content-disposition": "attachment; filename=raw_tables.json"}
    )

@app.route('/download/text', methods=['GET'])
def download_text():
    if not latest_data or not latest_data.get('text'):
        return "No text available", 404
        
    return Response(
        latest_data['text'],
        mimetype="text/plain",
        headers={"Content-disposition": "attachment; filename=extracted_text.txt"}
    )

if __name__ == '__main__':
    app.run(debug=True, port=8765)
