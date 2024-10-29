from flask import Flask, send_file, request, jsonify
import mysql.connector as sql
import pandas as pd
import os
import warnings
import cv2 as cv
import numpy as np
import tensorflow as tf
import threading
from datetime import datetime
from flask_cors import CORS
from werkzeug.utils import secure_filename

# Suppress warnings
warnings.filterwarnings("ignore")

# Create the Flask app (API)
app = Flask(__name__)
CORS(app)

# Connection settings (no persistent connection)
def create_connection():
    return sql.connect(
        host="localhost",
        database="finlove",
        user="root",
        password="1234"
    )

# Load AI model
MODEL_PATH = 'model_finlove.h5'
model = tf.keras.models.load_model(MODEL_PATH)

# Path to the folder where images are stored
IMAGE_FOLDER = os.path.join(os.getcwd(), 'assets', 'user')
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')

# Function to delete a file after a delay
def delete_file_after_delay(file_path, delay):
    def delete_file():
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"{file_path} has been deleted.")
    timer = threading.Timer(delay, delete_file)
    timer.start()

# Route for AI recommendations
@app.route('/ai/recommend/<int:id>', methods=['GET'])
def recommend(id):
    # สร้างการเชื่อมต่อใหม่ทุกครั้งที่เรียกใช้งาน
    conn = create_connection()
    
    # ดึงข้อมูลใหม่จากตาราง userpreferences ทุกครั้งที่มีการเรียกใช้งาน
    sql_query = "SELECT * FROM userpreferences"
    x = pd.read_sql(sql_query, conn)

    # ตรวจสอบให้แน่ใจว่า DataFrame มีคอลัมน์ที่จำเป็น
    if 'UserID' not in x.columns or 'PreferenceID' not in x.columns:
        return jsonify({"error": "Data format error in userpreferences table"}), 500

    # ปรับข้อมูลของ userpreferences ให้เป็น pivot table
    x = x.pivot_table(index='UserID', columns='PreferenceID', aggfunc='size', fill_value=0)

    # ตรวจสอบว่า UserID ที่ร้องขอมีอยู่ใน DataFrame หรือไม่
    if id not in x.index:
        return jsonify({"error": f"UserID {id} not found in preferences table"}), 404

    # แยกข้อมูลสำหรับผู้ใช้ที่ล็อกอินและผู้ใช้อื่น ๆ
    x_login_user = x.loc[[id]]  # ข้อมูลผู้ใช้ที่ล็อกอิน
    x_other_users = x.drop([id])  # ข้อมูลผู้ใช้อื่น ๆ

    # ตรวจสอบความเข้ากันของ preferences อย่างน้อย 1 รายการ
    recommended_user_ids = []
    for other_user_id, other_user_data in x_other_users.iterrows():
        common_preferences = (x_login_user.values[0] == other_user_data.values).sum()
        if common_preferences >= 1:
            recommended_user_ids.append(other_user_id)

    if len(recommended_user_ids) == 0:
        return jsonify({"message": "No similar users found"}), 200

    recommended_user_ids_str = ', '.join(map(str, recommended_user_ids))

    # ดึงข้อมูลผู้ใช้แนะนำที่ยังไม่ได้จับคู่หรือบล็อก
    sql_query = f'''
    SELECT 
        u.UserID, 
        u.nickname, 
        u.imageFile,
        u.verify
    FROM user u
    LEFT JOIN matches m ON (m.user1ID = u.UserID AND m.user2ID = {id}) OR (m.user2ID = u.UserID AND m.user1ID = {id})
    LEFT JOIN blocked_chats b ON (b.user1ID = {id} AND b.user2ID = u.UserID) OR (b.user2ID = {id} AND b.user1ID = u.UserID)
    WHERE u.UserID IN ({recommended_user_ids_str})
      AND m.matchID IS NULL
      AND (b.isBlocked IS NULL OR b.isBlocked = 0)
    '''

    recommended_users = pd.read_sql(sql_query, conn)
    conn.close()  # ปิดการเชื่อมต่อหลังจากดึงข้อมูลเสร็จ

    # ปรับเส้นทางของ imageFile เพื่อให้ชี้ไปที่ API สำหรับโหลดรูปภาพ
    for index, user in recommended_users.iterrows():
        if user['imageFile']:
            recommended_users.at[index, 'imageFile'] = f"http://{request.host}/api/user/{user['imageFile']}"

    return jsonify(recommended_users[['UserID', 'nickname', 'imageFile', 'verify']].to_dict(orient='records')), 200

# Route for AI verification
@app.route('/ai/predict', methods=['POST'])
def predict():
    if 'image' not in request.files or 'UserID' not in request.form:
        return jsonify({"error": "Missing image or UserID"}), 400

    file = request.files['image']
    user_id = request.form['UserID']

    filename = secure_filename(file.filename)
    allowed_extensions = {'png', 'jpg', 'jpeg'}
    if not ('.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return jsonify({"error": "Invalid file type"}), 400

    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)

    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    delete_file_after_delay(file_path, 180)

    try:
        image = tf.keras.preprocessing.image.load_img(file_path, target_size=(224, 224))
        image = tf.keras.preprocessing.image.img_to_array(image)
        image = tf.expand_dims(image, axis=0)
    except Exception as e:
        return jsonify({"error": f"Image processing error: {str(e)}"}), 500

    try:
        predictions = model.predict(image, verbose=0)
        predicted_class = int(tf.argmax(predictions, axis=1).numpy()[0])
        confidence_score = float(predictions[0][predicted_class])  # Convert to float for JSON serialization
    except Exception as e:
        return jsonify({"error": f"Model prediction error: {str(e)}"}), 500

    # กำหนดค่า is_human ตาม confidence_score โดยใช้เกณฑ์ใหม่
    is_human = confidence_score < 0.95  # ถ้า confidence_score น้อยกว่า 95 จะถือว่าเป็นมนุษย์
    verification_status = 1 if is_human else 0  # ถ้าเป็นมนุษย์จะเป็น 1, ถ้าไม่ใช่มนุษย์จะเป็น 0

    try:
        conn = create_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE user SET verify = %s WHERE UserID = %s", (verification_status, user_id))
        conn.commit()
        cursor.close()
        conn.close()
    except sql.Error as err:
        return jsonify({"error": f"Database error: {str(err)}"}), 500

    result = {
        "is_human": is_human,
        "confidence_score": round(confidence_score * 100, 2),
        "message": "Verification status updated successfully" if is_human else "Verification failed"
    }

    return jsonify(result), 200

# Route to get user image
@app.route('/api/user/<filename>', methods=['GET'])
def get_user_image(filename):
    # Full path to the image file
    image_path = os.path.join(IMAGE_FOLDER, filename)

    # Check if the file exists
    if os.path.exists(image_path):
        # Return the image file to the client
        return send_file(image_path, mimetype='image/jpeg')
    else:
        # If the file is not found, return 404
        return jsonify({"error": "File not found"}), 404

# Create Web server
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=6000)
