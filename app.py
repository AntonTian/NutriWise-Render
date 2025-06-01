from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from firebase_admin import credentials, auth, firestore, initialize_app
import firebase_admin as fa
from datetime import datetime
import re
from uuid import uuid4
from flask_mail import Mail, Message
import random
import time
from dotenv import load_dotenv
import os

app = Flask(__name__)
CORS(app, supports_credentials=True)

cred = credentials.Certificate("ServiceAccountKey.json")
fa.initialize_app(cred)

db = firestore.client()

load_dotenv(dotenv_path=".env")

#===============================================
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")

    if not email.endswith("@gmail.com"):
        return jsonify({"error": "Invalid email format. Only @gmail.com allowed."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password length must be at least 8 characters."}), 400

    try:
        # Check for duplicate username or email in Firebase Auth
        all_users = auth.list_users().users
        for user in all_users:
            if user.display_name == name:
                return jsonify({"error": "Username already exists"}), 409
            if user.email == email:
                return jsonify({"error": "Email already registered"}), 409

        # Create user in Firebase Authentication
        user = auth.create_user(email=email, password=password, display_name=name)

        # Save mapping of username -> uid to Firestore Users collection
        db.collection("Users").document(name).set({
            "email": email,
            "uid": user.uid,
            "created_at": firestore.SERVER_TIMESTAMP
        })

        return jsonify({
            "message": "User registered successfully",
            "uid": user.uid,
            "email": user.email
        }), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/login", methods=["POST"])
def login():
    import requests

    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    api_key = os.getenv('API_KEY')
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"

    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True
    }

    response = requests.post(url, json=payload)

    if response.status_code == 200:
        id_token = response.json().get("idToken")

        response_data = {
            "message": "Login successful",
            "token": id_token
        }

        flask_response = make_response(jsonify(response_data), 200)

        return flask_response
    else:
        return jsonify({"error": response.json()}), 401

@app.route("/user/<uid>", methods=["GET"])
def get_user(uid):
    try:
        user = auth.get_user(uid)
        return jsonify({
            "uid": user.uid,
            "email": user.email,
            "name": user.display_name
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 404

@app.route("/getAllUserName", methods=["GET"])
def get_all_usernames():
    users = auth.list_users().users
    usernames = [user.display_name for user in users if user.display_name]
    return jsonify(usernames), 200
#==============================================================================
# Ensure user exists with a food list
def ensure_user_foodlist(username):
    user_ref = db.collection("UserFoodLists").document(username)
    if not user_ref.get().exists:
        user_ref.set({"foods": []})
    return user_ref

# Get all foodnames (Need to fix)
@app.route("/getAllFoodNames", methods=["GET"])
def get_all_food_names():
    try:
        food_names = []

        food_docs = db.collection("FoodList").stream()

        for doc in food_docs:
            food_data = doc.to_dict()
            name = food_data.get("food_name")
            if name:
                food_names.append(name)

        return jsonify(sorted(set(food_names))), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Get food list based on username from request body
@app.route('/getFoodlistBasedUser', methods=['POST'])
def get_foodlist_based_user():
    data = request.get_json()
    username = data.get("username")
    if not username:
        return jsonify({"error": "Username is required"}), 400

    user_ref = ensure_user_foodlist(username)
    foods = user_ref.get().to_dict().get("foods", [])
    filtered_foods = [
        {
            "log_id": food["log_id"],
            "food_name": food["food_name"],
            "quantity": food["quantity"],
            "expiry_date": food["expiry_date"]
        }
        for food in foods if not food.get("isDeleted", False)
    ]
    return jsonify(filtered_foods), 200

# Add a new food item
@app.route('/addFood', methods=['POST'])
def add_food():
    data = request.get_json()
    required_fields = ['username', 'food_name', 'quantity', 'expiry_date']
    if not all(field in data for field in required_fields):
        return jsonify({"error": "Please Fill all the fields before adding!"}), 400

    username = data["username"]

    # 🔍 Lookup UID from Firestore "Users" collection
    user_meta = db.collection("Users").document(username).get()
    if not user_meta.exists:
        return jsonify({"error": "User not found"}), 404

    user_id = user_meta.to_dict().get("uid")  # UID from Firebase Auth

    raw_food_name = data["food_name"]
    normalized_food_name = raw_food_name.replace("-", " ").strip().title()

    user_ref = ensure_user_foodlist(username)
    foods = user_ref.get().to_dict().get("foods", [])

    for food in foods:
        if food["food_name"].lower() == data["food_name"].strip().lower() and not food.get("isDeleted", False):
            return jsonify({"error": "Food already exists"}), 400

    try:
        expiry_date = datetime.strptime(data["expiry_date"], "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    new_food = {
        "log_id": str(uuid4()),
        "food_name": normalized_food_name,
        "quantity": str(data['quantity']),
        "expiry_date": expiry_date.strftime("%Y-%m-%d"),
        "isDeleted": False
    }

    user_ref.update({"foods": firestore.ArrayUnion([new_food])})

    # 🔍 Log the action with UID
    db.collection("FoodLogs").add({
        "log_id": str(uuid4()),
        "user_id": user_id,
        "food_name": normalized_food_name,
        "quantity": new_food["quantity"],
        "expiry_date": new_food["expiry_date"],
        "date_up": firestore.SERVER_TIMESTAMP,
        "isDeleted": False
    })

    updated_foods = user_ref.get().to_dict().get("foods", [])
    return jsonify(updated_foods), 201

#delete food item
@app.route('/DeleteFood', methods=['PATCH'])
def delete_food():
    data = request.get_json()
    if "username" not in data or "log_id" not in data:
        return jsonify({"error": "Missing required fields: username and log_id"}), 400

    username = data["username"]
    log_id = data["log_id"]

    # Get reference to the user's food list
    user_ref = ensure_user_foodlist(username)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return jsonify({"error": "User food list not found"}), 404

    user_data = user_doc.to_dict() or {}
    foods = user_data.get("foods", [])

    updated = False

    for food in foods:
        # Ensure food has a log_id before comparing
        if str(food.get("log_id")) == str(log_id) and not food.get("isDeleted", False):
            food["isDeleted"] = True
            updated = True

            # Log the deletion to FoodLogs
            db.collection("FoodLogs").add({
                "log_id": log_id,
                "user_id": username,
                "food_name": food.get("food_name", ""),
                "quantity": food.get("quantity", ""),
                "expiry_date": food.get("expiry_date", ""),
                "date_up": firestore.SERVER_TIMESTAMP,
                "isDeleted": True,
            })
            break

    if not updated:
        return jsonify({"error": "No matching food found with provided log_id"}), 404

    # Save updated food list
    user_ref.update({"foods": foods})

    return jsonify({"message": f"Food with log_id '{log_id}' marked as deleted."}), 200

@app.route("/test-food", methods=["POST"])
def test_food_lookup():
    data = request.get_json()
    food_name = data.get("food_name")

    if not food_name:
        return jsonify({"error": "Missing food_name"}), 400

    food_docs = db.collection("FoodList").where("food_name", "==", food_name).stream()
    food_doc = next(food_docs, None)

    if not food_doc:
        return jsonify({"error": "Food not found"}), 404

    return jsonify(food_doc.to_dict()), 200
#===============================================================================
@app.route('/getCalorieBasedUser', methods=['POST'])
def get_calorie_based_user():
    data = request.get_json()
    username = data.get("username")
    if not username:
        return jsonify({"error": "Username is required"}), 400

    doc_ref = db.collection("CalorieSession").document(username)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "No calorie session found for this user"}), 404

    all_foods = doc.to_dict().get("foods", [])

    # Only include foods not marked as deleted
    filtered_foods = [
        {
            "log_id": food["log_id"],
            "food_name": food["food_name"],
            "quantity": food["quantity"],
            "calories": food["calories"],
            "carbs": food["carbs"],
            "protein": food["protein"],
            "fats": food["fats"]
        }
        for food in all_foods if not food.get("isDeleted", False)
    ]

    return jsonify(filtered_foods), 200


@app.route("/calorie/add", methods=["POST"])
def add_calorie_food():
    data = request.get_json()
    username = data.get("username")
    food_name = data.get("food_name")
    quantity = data.get("quantity")

    if not all([username, food_name, quantity]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        quantity = float(quantity)
    except ValueError:
        return jsonify({"error": "Quantity must be a number"}), 400

    # Get food info from FoodList collection
    food_docs = db.collection("FoodList").where("food_name", "==", food_name).stream()
    food_doc = next(food_docs, None)
    if not food_doc:
        return jsonify({"error": "Food not found"}), 404

    food_data = food_doc.to_dict()
    factor = quantity / 100

    entry = {
        "log_id": str(uuid4()),
        "food_name": food_name,
        "quantity": quantity,
        "calories": round(food_data.get("calories", 0) * factor, 2),
        "carbs": round(food_data.get("carbs", 0) * factor, 2),
        "protein": round(food_data.get("protein", 0) * factor, 2),
        "fats": round(food_data.get("fats", 0) * factor, 2),
        "isDeleted": False
    }

    doc_ref = db.collection("CalorieSession").document(username)
    doc = doc_ref.get()
    foods = doc.to_dict().get("foods", []) if doc.exists else []
    foods.append(entry)

    doc_ref.set({"foods": foods})

    return jsonify({"message": "Food added to calorie session", "foods": foods}), 200

@app.route("/calorie/delete", methods=["PATCH"])
def delete_calorie_food():
    data = request.get_json()
    username = data.get("username")
    log_id = data.get("log_id")

    if not username or not log_id:
        return jsonify({"error": "Missing username or log_id"}), 400

    doc_ref = db.collection("CalorieSession").document(username)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "No session found"}), 404

    foods = doc.to_dict().get("foods", [])
    updated = False

    for food in foods:
        if food.get("log_id") == log_id and not food.get("isDeleted", False):
            food["isDeleted"] = True
            updated = True
            break

    if not updated:
        return jsonify({"error": "No matching food found with provided log_id"}), 404

    doc_ref.set({"foods": foods})
    return jsonify({"message": f"Food with log_id '{log_id}' marked as deleted."}), 200

@app.route("/calorie/summary", methods=["POST"])
def get_calorie_summary():
    data = request.get_json()
    username = data.get("username")

    doc = db.collection("CalorieSession").document(username).get()
    if not doc.exists:
        return jsonify({"error": "No data found"}), 404

    all_foods = doc.to_dict().get("foods", [])

    # Filter out deleted foods
    active_foods = [f for f in all_foods if not f.get("isDeleted", False)]

    # summarize only non-deleted entries
    summary = {
        "calories": sum(f.get("calories", 0) for f in active_foods),
        "carbs": sum(f.get("carbs", 0) for f in active_foods),
        "protein": sum(f.get("protein", 0) for f in active_foods),
        "fats": sum(f.get("fats", 0) for f in active_foods)
    }

    return jsonify({
        "foods": active_foods,
        "summary": summary
    }), 200
#===========================================================================
def extract_ingredient_names(ingredient_lines):
    ingredient_names = set()

    descriptors_to_ignore = {"bruised", "sliced", "grated", "minced", "thinly", "julienned", "soaked", "cut into cubes"}
    for line in ingredient_lines:
        parts = line.split(";")
        for part in parts:
            part = part.strip().lower()

            # Remove quantity and unit prefixes
            part = re.sub(r'^\d+(\.\d+)?\s*(g|gram|grams|ml|cup[s]?|tbsp[s]?|tsp[s]?|inch|clove[s]?|slice[d]?|stalk[s]?|leaves?)?\s*', '', part)

            # Normalize 'to taste'
            part = re.sub(r'\bto taste\b', '', part).strip()

            part = part.replace("-", " ")

            # Remove descriptors like 'bruised', 'sliced', etc.
            words = [word for word in part.split() if word not in descriptors_to_ignore]
            cleaned_part = " ".join(words).strip()

            # Final cleanup
            cleaned_part = re.sub(r'[^\w\s]', '', cleaned_part).strip()

            if cleaned_part:
                ingredient_names.add(cleaned_part)

    return ingredient_names


@app.route('/getRecipesWithAvailability', methods=['POST'])
def get_recipes_with_availability():
    data = request.json
    username = data.get("username")

    if not username:
        return jsonify({"error": "Username is required"}), 400

    # Get all user's available food items, skipping deleted ones
    user_food_docs = db.collection("UserFoodLists").document(username).get()
    if not user_food_docs.exists:
        return jsonify({"error": "User not found"}), 404

    user_food_data = user_food_docs.to_dict().get("foods", [])
    user_ingredients_map = {
        item["food_name"].strip().lower(): item["food_name"].strip()
        for item in user_food_data if not item.get("isDeleted", False)
    }
    user_ingredients_lower = set(user_ingredients_map.keys())

    recipe_docs = db.collection("FoodList").stream()
    recipes = []

    for doc in recipe_docs:
        recipe = doc.to_dict()
        title = recipe.get("recipe_title")
        steps = recipe.get("recipe_steps")
        ingredient_lines = recipe.get("ingredients", [])

        parsed_ingredients = extract_ingredient_names(ingredient_lines)

        # Case-insensitive comparison using lowercased keys
        available = [user_ingredients_map[ing] for ing in parsed_ingredients if ing in user_ingredients_lower]
        unavailable = [ing.title() for ing in parsed_ingredients if ing not in user_ingredients_lower]  # Optional: title-case

        recipes.append({
            "recipe_title": title,
            "recipe_steps": steps,
            "ingredient_details": ingredient_lines,
            "available_ingredients": available,
            "unavailable_ingredients": unavailable,
            "total_ingredients": len(parsed_ingredients)
        })

    # Sort recipes by number of ingredients (descending)
    recipes.sort(key=lambda x: x["total_ingredients"], reverse=True)
    return jsonify({"recipes": recipes}), 200
#====================================================================================
#Forgot and reset password
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = (
    os.getenv('MAIL_DEFAULT_SENDER_NAME'),
    os.getenv('MAIL_DEFAULT_SENDER_EMAIL')
)

mail = Mail(app)

# In-memory store (replace with Firestore or Redis for production)
verification_codes = {}

def generate_verification_code():
    return str(random.randint(100000, 999999))

def send_verification_email(email, code):
    try:
        msg = Message(
            subject="Your Verification Code",
            recipients=[email],
            html=f"<strong>Your verification code is: {code}</strong>"
        )
        mail.send(msg)
        print(f"[DEBUG] Sent code {code} to {email}")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send email to {email}: {e}")
        return False

@app.route('/requestResetCode', methods=['POST'])
def request_reset_code():
    data = request.json
    email = data.get("email")

    if not email:
        return jsonify({"error": "Email is required"}), 400

    try:
        auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        return jsonify({"error": "No user found with that email"}), 404

    code = generate_verification_code()
    verification_codes[email] = {
        "code": code,
        "expires": time.time() + 300
    }

    if not send_verification_email(email, code):
        return jsonify({"error": "Failed to send verification code"}), 500

    return jsonify({"message": "Verification code sent to email"}), 200

@app.route('/verifyResetCode', methods=['POST'])
def verify_reset_code():
    data = request.json
    email = data.get("email")
    user_code = data.get("code")

    if not email or not user_code:
        return jsonify({"error": "Email and code are required"}), 400

    record = verification_codes.get(email)
    if not record:
        return jsonify({"error": "No reset request found for this email"}), 400

    if time.time() > record["expires"]:
        return jsonify({"error": "Verification code expired"}), 400

    if str(user_code) != record["code"]:
        return jsonify({"error": "Incorrect verification code"}), 400

    try:
        link = auth.generate_password_reset_link(email)
        return jsonify({
            "message": "Verification successful. Password reset link generated.",
            "reset_link": link
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)

