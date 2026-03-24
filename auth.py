# auth.py - Authentication Module
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()
# MongoDB connection
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
mongo_client = MongoClient(MONGO_URI)
db = mongo_client['support_system_db']

# Collections
users_collection = db['users']
admins_collection = db['admins']
sessions_collection = db['sessions']


class Auth:
    """Authentication and user management"""
    
    @staticmethod
    def create_user(email, password, name, role='user', phone=None):
        """Create a new user or admin"""
        # Check if user already exists
        existing = users_collection.find_one(
            {"$or": [{"email": email}, {"phone": phone}]} if phone else {"email": email}
        )
        if existing:
            return {"error": "Email already exists"}, False
        
        user = {
            "email": email,
            "password": generate_password_hash(password),
            "name": name,
            "role": role,  # 'user', 'support', 'admin'
            "active": True,
            "created_at": datetime.now(),
            "last_login": None
            ,
            "phone": phone or ""
        }
        
        result = users_collection.insert_one(user)
        return str(result.inserted_id), True
    
    @staticmethod
    def authenticate(email=None, password=None, phone=None):
        """Authenticate user by email or phone"""
        lookup = None
        if phone:
            lookup = {"phone": phone, "active": True}
        elif email:
            lookup = {"email": email, "active": True}
        else:
            return {"error": "Email or phone is required"}, False

        user = users_collection.find_one(lookup)
        
        if user and check_password_hash(user['password'], password):
            # Update last login
            users_collection.update_one(
                {"_id": user['_id']},
                {"$set": {"last_login": datetime.now()}}
            )
            
            return {
                "user_id": str(user['_id']),
                "name": user['name'],
                "email": user['email'],
                "role": user.get('role', 'user'),
                "phone": user.get('phone', '')
            }, True
        
        return {"error": "Invalid credentials"}, False
    
    @staticmethod
    def get_user_by_id(user_id):
        """Get user by MongoDB ObjectId"""
        try:
            user = users_collection.find_one({"_id": ObjectId(user_id), "active": True})
            if user:
                return {
                    "user_id": str(user['_id']),
                    "name": user['name'],
                    "email": user['email'],
                    "role": user.get('role', 'user'),
                    "phone": user.get('phone', '')
                }, True
        except:
            pass
        return {"error": "User not found"}, False
    
    @staticmethod
    def get_all_admins():
        """Get all admin and support users"""
        admins = users_collection.find(
            {"role": {"$in": ["admin", "support"]}, "active": True},
            {"password": 0}  # Exclude password
        )
        return list(admins)
    
    @staticmethod
    def get_all_users():
        """Get all regular users"""
        users = users_collection.find(
            {"role": "user", "active": True},
            {"password": 0}  # Exclude password
        )
        return list(users)
    
    @staticmethod
    def update_user_status(user_id, active):
        """Activate or deactivate user"""
        try:
            result = users_collection.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"active": active}}
            )
            return result.modified_count > 0
        except:
            return False
    
    @staticmethod
    def change_password(user_id, old_password, new_password):
        """Change user password"""
        try:
            user = users_collection.find_one({"_id": ObjectId(user_id)})
            if user and check_password_hash(user['password'], old_password):
                users_collection.update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {"password": generate_password_hash(new_password)}}
                )
                return True
        except:
            pass
        return False


# Initialize with default admin if no users exist
def init_default_admin():
    """Create default admin user if none exists"""
    # admin_count = users_collection.count_documents({"role": "admin"})

    # if admin_count == 0:
    #     print("Creating default admin user...")
    #     default_admin = {
    #         "email": "a.com",
    #         "password": generate_password_hash(""),  # Change this!
    #         "name": "System Admin",
    #         "role": "admin",
    #         "active": True,
    #         "created_at": datetime.now(),
    #         "last_login": None
    #     }
    #     result = users_collection.insert_one(default_admin)
    #     print(f"⚠️  Please change the password immediately!")
    #     return str(result.inserted_id)
    return None

