"""
JWT Authentication Module
Provides stateless authentication using JSON Web Tokens
"""

import jwt
import os
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify
from auth import Auth
from dotenv import load_dotenv
load_dotenv()
# JWT Configuration
JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'your-secret-jwt-key-change-this-in-production')
JWT_ALGORITHM = 'HS256'
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)  # 24 hours
JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)  # 30 days


class JWTAuth:
    """JWT Authentication Handler"""
    
    @staticmethod
    def generate_access_token(user_id, email, name, role):
        """Generate JWT access token"""
        payload = {
            'user_id': user_id,
            'email': email,
            'name': name,
            'role': role,
            'type': 'access',
            'exp': datetime.utcnow() + JWT_ACCESS_TOKEN_EXPIRES,
            'iat': datetime.utcnow()
        }
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token
    
    @staticmethod
    def generate_refresh_token(user_id):
        """Generate JWT refresh token"""
        payload = {
            'user_id': user_id,
            'type': 'refresh',
            'exp': datetime.utcnow() + JWT_REFRESH_TOKEN_EXPIRES,
            'iat': datetime.utcnow()
        }
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token
    
    @staticmethod
    def verify_token(token, token_type='access'):
        """Verify and decode JWT token"""
        try:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            
            # Check token type
            if payload.get('type') != token_type:
                return None, "Invalid token type"
            
            return payload, None
        except jwt.ExpiredSignatureError:
            return None, "Token has expired"
        except jwt.InvalidTokenError as e:
            return None, f"Invalid token: {str(e)}"
    
    @staticmethod
    def refresh_access_token(refresh_token):
        """Generate new access token from refresh token"""
        payload, error = JWTAuth.verify_token(refresh_token, token_type='refresh')
        
        if error:
            return None, error
        
        # Get user from database
        user_data, success = Auth.get_user_by_id(payload['user_id'])
        
        if not success:
            return None, "User not found"
        
        # Generate new access token
        new_access_token = JWTAuth.generate_access_token(
            user_data['user_id'],
            user_data['email'],
            user_data['name'],
            user_data['role']
        )
        
        return new_access_token, None
    
    @staticmethod
    def decode_token_from_request():
        """Extract and decode JWT from request"""
        # Try to get token from Authorization header
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            return JWTAuth.verify_token(token)
        
        # Try to get token from cookie
        token = request.cookies.get('access_token')
        if token:
            return JWTAuth.verify_token(token)
        
        return None, "No token provided"


def jwt_required(f):
    """Decorator to require valid JWT token"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        payload, error = JWTAuth.decode_token_from_request()
        
        if error:
            return jsonify({'error': error}), 401
        
        # Add user data to request context
        request.user = payload
        return f(*args, **kwargs)
    
    return decorated_function


def jwt_admin_required(f):
    """Decorator to require admin or support role"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        payload, error = JWTAuth.decode_token_from_request()
        
        if error:
            return jsonify({'error': error}), 401
        
        # Check role
        if payload.get('role') not in ['admin', 'support']:
            return jsonify({'error': 'Admin or support role required'}), 403
        
        # Add user data to request context
        request.user = payload
        return f(*args, **kwargs)
    
    return decorated_function


def jwt_optional(f):
    """Decorator for optional JWT authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        payload, error = JWTAuth.decode_token_from_request()
        
        # Set user data if token is valid, otherwise None
        request.user = payload if not error else None
        return f(*args, **kwargs)
    
    return decorated_function


# SocketIO JWT validation
def validate_socketio_token(token):
    """Validate JWT token for SocketIO connections"""
    if not token:
        return None, "No token provided"
    
    payload, error = JWTAuth.verify_token(token)
    
    if error:
        return None, error
    
    return payload, None


