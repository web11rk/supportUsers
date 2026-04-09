from flask import Flask, render_template, request, redirect, url_for, session, jsonify, render_template_string, make_response, Response
from flask_socketio import SocketIO, send, join_room, leave_room, emit
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os
import datetime
from collections import deque
from enum import Enum
import redis
from flask_session import Session
from functools import wraps
from auth import Auth, init_default_admin
from jwt_auth import JWTAuth, jwt_required, jwt_admin_required, validate_socketio_token
import requests

from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)
# Use environment variable for SECRET_KEY (persistent across restarts)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-secret-key-in-production-make-it-persistent')

# Rate Limiting Configuration
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["10000 per day", "500 per hour"],
    storage_uri=os.getenv('REDIS_URL','redis://default:lmNHNCLBbLimIWuRlHOipVzEbOEpxcJY@maglev.proxy.rlwy.net:36764' )#'redis://localhost:6379'
)

# Authentication decorators (support both JWT and Session)
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Try JWT first
        payload, error = JWTAuth.decode_token_from_request()
        if not error:
            request.user = payload
            return f(*args, **kwargs)
        
        # Fall back to session
        if 'user_id' not in session:
            return redirect(url_for('index'))
        
        # Create request.user from session for consistency
        request.user = {
            'user_id': session.get('user_id'),
            'name': session.get('username'),
            'email': session.get('email'),
            'role': session.get('role')
        }
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Try JWT first
        payload, error = JWTAuth.decode_token_from_request()
        if not error:
            if payload.get('role') not in ['admin', 'support']:
                return redirect(url_for('index'))
            request.user = payload
            return f(*args, **kwargs)
        
        # Fall back to session
        if 'role' not in session or session['role'] not in ['admin', 'support']:
            return redirect(url_for('index'))
        
        request.user = {
            'user_id': session.get('user_id'),
            'name': session.get('username'),
            'email': session.get('email'),
            'role': session.get('role')
        }
        return f(*args, **kwargs)
    return decorated_function

# Redis configuration for session management
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_REDIS'] = redis.from_url('redis://default:lmNHNCLBbLimIWuRlHOipVzEbOEpxcJY@maglev.proxy.rlwy.net:36764')
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'support_system:'

# Initialize session
Session(app)

socketio = SocketIO(app, cors_allowed_origins="*")


# ==================== DATA MODELS ====================

class TicketStatus(Enum):
    OPEN = "Open"
    IN_PROGRESS = "In Progress"
    RESOLVED = "Resolved"
    CLOSED = "Closed"


class Priority(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class Ticket:
    ticket_counter = 1000

    def __init__(self, user_name, subject, description, priority="MEDIUM", user_id=None):
        Ticket.ticket_counter += 1
        self.ticket_id = f"TKT-{Ticket.ticket_counter}"
        self.user_name = user_name
        self.user_id = user_id  # MongoDB ObjectId or user ID
        self.subject = subject
        self.description = description
        self.priority = priority
        self.status = TicketStatus.OPEN.value
        self.created_at = datetime.datetime.now().isoformat()
        self.updated_at = self.created_at
        self.messages = []
        self.assigned_to = None
        self.room_id = self.ticket_id

    def add_message(self, sender, message, sender_type="user"):
        msg = {
            'sender': sender,
            'message': message,
            'sender_type': sender_type,
            'timestamp': datetime.datetime.now().isoformat()
        }
        self.messages.append(msg)
        self.updated_at = datetime.datetime.now().isoformat()
        return msg

    def update_status(self, new_status):
        self.status = new_status
        self.updated_at = datetime.datetime.now().isoformat()

    def to_dict(self):
        return {
            'ticket_id': self.ticket_id,
            'user_name': self.user_name,
            'user_id': self.user_id,
            'subject': self.subject,
            'description': self.description,
            'priority': self.priority,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'messages': self.messages,
            'assigned_to': self.assigned_to,
            'room_id': self.room_id
        }

    @staticmethod
    def from_dict(data):
        """Create Ticket object from dictionary"""
        ticket = Ticket.__new__(Ticket)
        ticket.ticket_id = data['ticket_id']
        ticket.user_name = data['user_name']
        ticket.user_id = data.get('user_id')
        ticket.subject = data['subject']
        ticket.description = data['description']
        ticket.priority = data['priority']
        ticket.status = data['status']
        ticket.created_at = data['created_at']
        ticket.updated_at = data['updated_at']
        ticket.messages = data.get('messages', [])
        ticket.assigned_to = data.get('assigned_to')
        ticket.room_id = data.get('room_id', ticket.ticket_id)
        return ticket


# ==================== REDIS SESSION MANAGER ====================

class RedisSessionManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.session_prefix = "support_system:session:"
        self.user_prefix = "support_system:user:"
        self.max_sessions_per_user = 3  # Limit sessions per user
        self.session_timeout = 3600  # 1 hour in seconds
        
    def create_session(self, sid, username, role="user"):
        """Create a new session in Redis"""
        session_data = {
            "username": username,
            "role": role,
            "created_at": datetime.datetime.now().isoformat(),
            "last_activity": datetime.datetime.now().isoformat()
        }
        
        # Store session data
        self.redis.hset(f"{self.session_prefix}{sid}", mapping=session_data)
        self.redis.expire(f"{self.session_prefix}{sid}", self.session_timeout)
        
        # Store user -> session mapping
        self.redis.sadd(f"{self.user_prefix}{username}:sessions", sid)
        self.redis.expire(f"{self.user_prefix}{username}:sessions", self.session_timeout)
        
        # Check and limit sessions per user
        self._limit_user_sessions(username)
        
        return session_data
    
    def get_session(self, sid):
        """Get session data from Redis"""
        session_data = self.redis.hgetall(f"{self.session_prefix}{sid}")
        if session_data:
            # Update last activity
            self.redis.hset(f"{self.session_prefix}{sid}", "last_activity", 
                           datetime.datetime.now().isoformat())
            self.redis.expire(f"{self.session_prefix}{sid}", self.session_timeout)
        return session_data
    
    def delete_session(self, sid):
        """Delete a session from Redis"""
        session_data = self.get_session(sid)
        if session_data:
            username = session_data.get("username")
            if username:
                # Remove from user's session list
                self.redis.srem(f"{self.user_prefix}{username}:sessions", sid)
                # Clean up empty user session set
                if self.redis.scard(f"{self.user_prefix}{username}:sessions") == 0:
                    self.redis.delete(f"{self.user_prefix}{username}:sessions")
        
        # Delete session data
        self.redis.delete(f"{self.session_prefix}{sid}")
    
    def get_user_sessions(self, username):
        """Get all sessions for a user"""
        return self.redis.smembers(f"{self.user_prefix}{username}:sessions")
    
    def _limit_user_sessions(self, username):
        """Limit the number of sessions per user"""
        user_sessions = self.get_user_sessions(username)
        if len(user_sessions) > self.max_sessions_per_user:
            # Get oldest sessions (by creation time)
            sessions_with_time = []
            for sid in user_sessions:
                session_data = self.redis.hgetall(f"{self.session_prefix}{sid.decode()}")
                if session_data:
                    sessions_with_time.append((sid.decode(), session_data.get("created_at", "")))
            
            # Sort by creation time and remove oldest
            sessions_with_time.sort(key=lambda x: x[1])
            sessions_to_remove = sessions_with_time[:-self.max_sessions_per_user]
            
            for sid, _ in sessions_to_remove:
                self.delete_session(sid)
    
    def cleanup_expired_sessions(self):
        """Clean up expired sessions (called periodically) - Redis Cluster compatible"""
        # Redis TTL handles expiration, but we can clean up user session sets
        pattern = f"{self.user_prefix}*:sessions"
        
        # Limit to process max 100 keys per cleanup cycle to prevent blocking
        keys_processed = 0
        max_keys_per_cycle = 100
        
        for key in self.redis.scan_iter(match=pattern, count=50):
            if keys_processed >= max_keys_per_cycle:
                break
            
            keys_processed += 1
            
            # Check if any sessions in the set are still valid
            sessions = self.redis.smembers(key)
            if not sessions:
                self.redis.delete(key)
                continue
            
            # CLUSTER-SAFE: Check sessions individually (works in both standalone and cluster)
            # Alternative: Use pipeline with try/except for cluster compatibility
            valid_sessions = []
            
            try:
                # Try to use pipeline (works in standalone Redis)
                pipe = self.redis.pipeline(transaction=False)  # transaction=False for cluster compatibility
                session_list = []
                for sid in sessions:
                    session_key = f"{self.session_prefix}{sid.decode()}"
                    pipe.exists(session_key)
                    session_list.append(sid)
                
                # Execute all EXISTS checks at once
                existence_results = pipe.execute()
                
                # Filter valid sessions
                valid_sessions = [
                    session_list[i] for i, exists in enumerate(existence_results) if exists
                ]
            except Exception as e:
                # Fallback for Redis Cluster or if pipeline fails
                print(f"Pipeline failed (cluster mode?), using individual queries: {e}")
                for sid in sessions:
                    session_key = f"{self.session_prefix}{sid.decode()}"
                    if self.redis.exists(session_key):
                        valid_sessions.append(sid)
            
            # Update the set with only valid sessions (or delete if empty)
            if valid_sessions:
                try:
                    pipe = self.redis.pipeline(transaction=False)
                    pipe.delete(key)
                    pipe.sadd(key, *valid_sessions)
                    pipe.execute()
                except Exception:
                    # Fallback for cluster mode
                    self.redis.delete(key)
                    self.redis.sadd(key, *valid_sessions)
            else:
                self.redis.delete(key)
    
    def get_user_sid(self, username):
        """Get the most recent session ID for a user"""
        sessions = self.get_user_sessions(username)
        if not sessions:
            return None
        
        # Get the most recent session
        latest_session = None
        latest_time = ""
        for sid in sessions:
            session_data = self.redis.hgetall(f"{self.session_prefix}{sid.decode()}")
            if session_data:
                created_at = session_data.get("created_at", "")
                if created_at > latest_time:
                    latest_time = created_at
                    latest_session = sid.decode()
        
        return latest_session


# ==================== REDIS TICKET MANAGER ====================

class RedisTicketManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.ticket_prefix = "support_system:ticket:"
        self.ticket_list_key = "support_system:tickets:all"
        self.ticket_counter_key = "support_system:ticket_counter"
        self.user_tickets_prefix = "support_system:user_tickets:"
        
        # Ticket status indexes for filtering
        self.active_tickets_key = "support_system:tickets:active"  # Open + In Progress
        self.resolved_tickets_key = "support_system:tickets:resolved"  # Resolved + Closed
        
        # TTL for resolved tickets (optional - 0 means keep forever)
        self.resolved_ticket_ttl = int(os.getenv('RESOLVED_TICKET_TTL', 2592000))  # 30 days default
        
        # Initialize counter from Redis or start at 1000
        if not self.redis.exists(self.ticket_counter_key):
            self.redis.set(self.ticket_counter_key, 1000)
    
    def create_ticket(self, user_name, subject, description, priority="MEDIUM", user_id=None):
        """Create a new ticket and store in Redis"""
        # Increment counter
        counter = self.redis.incr(self.ticket_counter_key)
        ticket_id = f"TKT-{counter}"
        
        # Create ticket dict
        ticket_data = {
            'ticket_id': ticket_id,
            'user_name': user_name,
            'user_id': user_id or '',
            'subject': subject,
            'description': description,
            'priority': priority,
            'status': TicketStatus.OPEN.value,
            'created_at': datetime.datetime.now().isoformat(),
            'updated_at': datetime.datetime.now().isoformat(),
            'messages': '[]',  # Store as JSON string
            'assigned_to': '',
            'room_id': ticket_id
        }
        
        # Store in Redis
        self.redis.hset(f"{self.ticket_prefix}{ticket_id}", mapping=ticket_data)
        
        # Add to global ticket list
        self.redis.sadd(self.ticket_list_key, ticket_id)
        
        # Add to active tickets index (for fast filtering)
        self.redis.sadd(self.active_tickets_key, ticket_id)
        
        # Add to user's ticket list
        self.redis.sadd(f"{self.user_tickets_prefix}{user_name}", ticket_id)
        
        # If user_id provided, also index by user_id
        if user_id:
            self.redis.sadd(f"{self.user_tickets_prefix}uid:{user_id}", ticket_id)
        
        # Create Ticket object
        ticket_data['messages'] = []
        return Ticket.from_dict(ticket_data)
    
    def get_ticket(self, ticket_id):
        """Get a ticket from Redis"""
        ticket_data = self.redis.hgetall(f"{self.ticket_prefix}{ticket_id}")
        if not ticket_data:
            return None
        
        # Decode and parse
        ticket_dict = {}
        for key, value in ticket_data.items():
            key_str = key.decode() if isinstance(key, bytes) else key
            value_str = value.decode() if isinstance(value, bytes) else value
            ticket_dict[key_str] = value_str
        
        # Parse messages JSON
        import json
        ticket_dict['messages'] = json.loads(ticket_dict.get('messages', '[]'))
        
        return Ticket.from_dict(ticket_dict)
    
    def update_ticket(self, ticket):
        """Update a ticket in Redis and manage status indexes"""
        import json
        ticket_dict = ticket.to_dict()
        
        # Convert messages to JSON string
        redis_data = ticket_dict.copy()
        redis_data['messages'] = json.dumps(ticket_dict['messages'])
        redis_data['assigned_to'] = redis_data['assigned_to'] or ''
        
        # Store in Redis
        self.redis.hset(f"{self.ticket_prefix}{ticket.ticket_id}", mapping=redis_data)
        
        # Update status indexes based on current status
        status = ticket.status
        if status in ['Open', 'In Progress']:
            # Move to active index (remove from resolved if it was there)
            self.redis.sadd(self.active_tickets_key, ticket.ticket_id)
            self.redis.srem(self.resolved_tickets_key, ticket.ticket_id)
        elif status in ['Resolved', 'Closed']:
            # Move to resolved index (remove from active)
            self.redis.srem(self.active_tickets_key, ticket.ticket_id)
            self.redis.sadd(self.resolved_tickets_key, ticket.ticket_id)
            
            # Set TTL on resolved tickets (optional - 0 means keep forever)
            if self.resolved_ticket_ttl > 0:
                self.redis.expire(f"{self.ticket_prefix}{ticket.ticket_id}", self.resolved_ticket_ttl)
    
    def get_all_tickets(self):
        """Get all tickets from Redis"""
        ticket_ids = self.redis.smembers(self.ticket_list_key)
        tickets = []
        for tid in ticket_ids:
            tid_str = tid.decode() if isinstance(tid, bytes) else tid
            ticket = self.get_ticket(tid_str)
            if ticket:
                tickets.append(ticket)
        return tickets
    
    def get_user_tickets(self, user_name):
        """Get all tickets for a specific user"""
        ticket_ids = self.redis.smembers(f"{self.user_tickets_prefix}{user_name}")
        tickets = []
        for tid in ticket_ids:
            tid_str = tid.decode() if isinstance(tid, bytes) else tid
            ticket = self.get_ticket(tid_str)
            if ticket:
                tickets.append(ticket)
        return tickets
    
    def get_pending_tickets(self):
        """Get all open/in-progress tickets"""
        all_tickets = self.get_all_tickets()
        return [t for t in all_tickets 
                if t.status in [TicketStatus.OPEN.value, TicketStatus.IN_PROGRESS.value]]
    
    def get_tickets_by_user_id(self, user_id):
        """Get all tickets for a specific user_id (MongoDB ID)"""
        ticket_ids = self.redis.smembers(f"{self.user_tickets_prefix}uid:{user_id}")
        tickets = []
        for tid in ticket_ids:
            tid_str = tid.decode() if isinstance(tid, bytes) else tid
            ticket = self.get_ticket(tid_str)
            if ticket:
                tickets.append(ticket)
        return tickets
    
    def get_active_tickets(self):
        """Get all active tickets (Open + In Progress) - Fast using index"""
        ticket_ids = self.redis.smembers(self.active_tickets_key)
        tickets = []
        for tid in ticket_ids:
            tid_str = tid.decode() if isinstance(tid, bytes) else tid
            ticket = self.get_ticket(tid_str)
            if ticket:
                tickets.append(ticket)
        return tickets
    
    def get_resolved_tickets(self):
        """Get all resolved/closed tickets - Fast using index"""
        ticket_ids = self.redis.smembers(self.resolved_tickets_key)
        tickets = []
        for tid in ticket_ids:
            tid_str = tid.decode() if isinstance(tid, bytes) else tid
            ticket = self.get_ticket(tid_str)
            if ticket:
                tickets.append(ticket)
        return tickets
    
    def get_tickets_by_status(self, status_filter='all'):
        """Get tickets filtered by status
        Args:
            status_filter: 'all', 'active', 'resolved', 'open', 'in_progress', 'closed'
        """
        if status_filter == 'active':
            return self.get_active_tickets()
        elif status_filter == 'resolved':
            return self.get_resolved_tickets()
        elif status_filter == 'all':
            return self.get_all_tickets()
        else:
            # Specific status filter
            all_tickets = self.get_all_tickets()
            return [t for t in all_tickets if t.status.lower().replace(' ', '_') == status_filter.lower()]
    
    def get_ticket_stats(self):
        """Get statistics about tickets"""
        return {
            'total': self.redis.scard(self.ticket_list_key),
            'active': self.redis.scard(self.active_tickets_key),
            'resolved': self.redis.scard(self.resolved_tickets_key)
        }


# ==================== REDIS READ STATUS MANAGER ====================

class RedisReadStatusManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.unread_count_prefix = "support_system:unread_count:"
    
    def increment_unread_count(self, ticket_id, user_identifier):
        """Increment unread count when new message arrives"""
        key = f"{self.unread_count_prefix}{ticket_id}:{user_identifier}"
        count = self.redis.incr(key)
        print(f"📬 Incremented unread count for {ticket_id} by {user_identifier}: {count}")
        return count
    
    def reset_unread_count(self, ticket_id, user_identifier):
        """Reset unread count to 0 when ticket is read"""
        key = f"{self.unread_count_prefix}{ticket_id}:{user_identifier}"
        self.redis.delete(key)
        print(f"✅ Reset unread count for {ticket_id} by {user_identifier}")
    
    def get_unread_count(self, ticket, user_identifier):
        """Get unread count for a ticket"""
        key = f"{self.unread_count_prefix}{ticket.ticket_id}:{user_identifier}"
        count = self.redis.get(key)
        if count:
            count = int(count.decode() if isinstance(count, bytes) else count)
        else:
            # If no counter exists, return 0
            # Counter is only created when messages are sent
            # If no counter = no unread messages (ticket is read or no messages yet)
            count = 0
        return count


# ==================== IN-MEMORY STORAGE ====================

ticket_queue = deque()
support_person = {"name": "Support Agent", "sid": None, "online": False}

# Initialize Redis clients and managers
# Redis Setup (supports both standalone and cluster)
REDIS_CLUSTER_ENABLED = os.getenv('REDIS_CLUSTER_ENABLED', 'false').lower() == 'true'

if REDIS_CLUSTER_ENABLED:
    # Redis Cluster mode
    try:
        from rediscluster import RedisCluster
        cluster_nodes = os.getenv('REDIS_CLUSTER_NODES', 'localhost:7000,localhost:7001,localhost:7002')
        startup_nodes = [{"host": node.split(':')[0], "port": int(node.split(':')[1])} 
                        for node in cluster_nodes.split(',')]
        redis_client = RedisCluster(startup_nodes=startup_nodes, decode_responses=False, skip_full_coverage_check=True)
        print("✅ Redis Cluster mode enabled")
    except ImportError:
        print("⚠️ redis-py-cluster not installed. Install with: pip install redis-py-cluster")
        print("Falling back to standalone Redis...")
        redis_client = redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379'))
else:
    # Standalone Redis (default)
    redis_client = redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379'))
    print("✅ Standalone Redis mode")

redis_client = "redis://default:lmNHNCLBbLimIWuRlHOipVzEbOEpxcJY@maglev.proxy.rlwy.net:36764"
session_manager = RedisSessionManager(redis_client)
ticket_manager = RedisTicketManager(redis_client)
read_status_manager = RedisReadStatusManager(redis_client)


# ==================== HELPER FUNCTIONS ====================

def get_pending_tickets():
    """Get pending tickets from Redis"""
    return ticket_manager.get_pending_tickets()


def get_user_tickets(user_name):
    """Get user tickets from Redis"""
    tickets = ticket_manager.get_user_tickets(user_name)
    return [t.to_dict() for t in tickets]


def get_all_tickets():
    """Get all tickets from Redis"""
    tickets = ticket_manager.get_all_tickets()
    return [t.to_dict() for t in tickets]


def notify_support_person(event, data):
    """Notify support person about events"""
    if support_person["online"] and support_person["sid"]:
        # Validate support session is still active
        session_data = session_manager.get_session(support_person["sid"])
        if session_data:
            socketio.emit(event, data, room=support_person["sid"])
        else:
            # Session expired, mark support as offline
            support_person["online"] = False
            support_person["sid"] = None


# ==================== HTML TEMPLATES ====================




# ==================== ROUTES ====================

@app.route('/')
def index():
    email = request.args.get('email')
    if email:
        # Validate email format
        import re
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            return render_template('index.html', error="Please enter a valid email address")

        # Extract name from email (before @)
        name = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()

        # Store user info in session (no authentication needed)
        session['user_id'] = f"guest_{email}"  # Guest user ID
        session['username'] = email  # Use email as username
        session['email'] = email
        session['role'] = 'guest'  # Mark as guest user

        # Redirect to user dashboard
        return redirect(url_for('user_dashboard'))
    else:
        return render_template('index.html')


@app.route('/admin-login')
def admin_login():
    """Separate admin login page (not publicly linked)"""
    return render_template('admin_login.html')


@app.route('/user/quick-access', methods=['POST'])
@limiter.limit("15 per minute")
def quick_access():
    """Quick access for users - no login required, just email"""
    email = request.form.get('email')
    
    if not email:
        return render_template('index.html', error="Email is required")
    
    # Validate email format
    import re
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return render_template('index.html', error="Please enter a valid email address")
    
    # Extract name from email (before @)
    name = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
    
    # Store user info in session (no authentication needed)
    session['user_id'] = f"guest_{email}"  # Guest user ID
    session['username'] = email  # Use email as username
    session['email'] = email
    session['role'] = 'guest'  # Mark as guest user
    
    # Redirect to user dashboard
    return redirect(url_for('user_dashboard'))


@app.route('/login', methods=['POST'])
@limiter.limit("10 per minute")
def login():
    email = request.form.get('email')
    password = request.form.get('password')
    
    if not email or not password:
        return render_template('index.html', error="Email and password are required")
    
    # Authenticate with MongoDB
    user_data, success = Auth.authenticate(email, password)
    
    if not success:
        return render_template('index.html', error="Invalid credentials")
    
    # Generate JWT tokens
    access_token = JWTAuth.generate_access_token(
        user_data['user_id'],
        user_data['email'],
        user_data['name'],
        user_data['role']
    )
    refresh_token = JWTAuth.generate_refresh_token(user_data['user_id'])
    
    # Store user info in session (for backward compatibility)
    session['user_id'] = user_data['user_id']
    session['username'] = user_data['name']
    session['email'] = user_data['email']
    session['role'] = user_data['role']
    
    # Determine redirect URL
    if user_data['role'] in ['admin', 'support']:
        support_person['name'] = user_data['name']
        redirect_url = url_for('support_dashboard')
    else:
        redirect_url = url_for('user_dashboard')
    
    # Create response with JWT cookies
    response = make_response(redirect(redirect_url))
    
    # Set HTTP-only cookies for tokens (secure against XSS)
    response.set_cookie(
        'access_token',
        access_token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite='Lax',
        max_age=60 * 60 * 24  # 24 hours
    )
    response.set_cookie(
        'refresh_token',
        refresh_token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite='Lax',
        max_age=60 * 60 * 24 * 30  # 30 days
    )
    
    return response


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    
    # Clear JWT cookies
    response = make_response(redirect(url_for('index')))
    response.set_cookie('access_token', '', expires=0)
    response.set_cookie('refresh_token', '', expires=0)
    
    return response


@app.route('/api/refresh-token', methods=['POST'])
def refresh_token_endpoint():
    """Refresh access token using refresh token"""
    refresh_token = request.cookies.get('refresh_token')
    
    if not refresh_token:
        return jsonify({'error': 'No refresh token provided'}), 401
    
    # Generate new access token
    new_access_token, error = JWTAuth.refresh_access_token(refresh_token)
    
    if error:
        return jsonify({'error': error}), 401
    
    # Return new access token
    response = jsonify({'message': 'Token refreshed successfully'})
    response.set_cookie(
        'access_token',
        new_access_token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite='Lax',
        max_age=60 * 60 * 24  # 24 hours
    )
    
    return response


@app.route('/user')
@login_required
def user_dashboard():
    # Use request.user which works with both JWT and session
    user = request.user
    return render_template('user_dashboard.html',
                         username=user['name'],
                         user_id=user['user_id'])


@app.route('/support')
@admin_required
def support_dashboard():
    # Use request.user which works with both JWT and session
    user = request.user
    # Use new WhatsApp-style UI
    return render_template('support_dashboard_v2.html', 
                         username=user['name'],
                         user_id=user['user_id'])

@app.route('/support/classic')
@admin_required
def support_dashboard_classic():
    # Use request.user which works with both JWT and session
    user = request.user
    # Use classic UI (old version)
    return render_template('support_dashboard.html', username=user['name'])


@app.route('/api/user/tickets')
@limiter.limit("30 per minute")
def api_user_tickets():
    username = request.args.get('username')
    user_email = request.args.get('user_email', username)  # For unread counts
    tickets_data = get_user_tickets(username)
    
    # Add unread counts
    for ticket_data in tickets_data:
        ticket = ticket_manager.get_ticket(ticket_data['ticket_id'])
        if ticket:
            ticket_data['unread_count'] = read_status_manager.get_unread_count(ticket, user_email)
    
    return jsonify(tickets_data)


@app.route('/api/support/tickets')
@limiter.limit("60 per minute")
def api_support_tickets():
    support_email = request.args.get('support_email', 'support')  # For unread counts
    status_filter = request.args.get('status', 'active')  # Filter: 'all', 'active', 'resolved'
    
    # Get tickets based on filter (defaults to active for performance)
    if status_filter == 'active':
        tickets = ticket_manager.get_active_tickets()
    elif status_filter == 'resolved':
        tickets = ticket_manager.get_resolved_tickets()
    elif status_filter == 'all':
        tickets = ticket_manager.get_all_tickets()
    else:
        tickets = ticket_manager.get_tickets_by_status(status_filter)
    
    tickets_data = [t.to_dict() for t in tickets]
    
    # Add unread counts for support
    # Use 'support' as identifier (all support agents see same count)
    for ticket_data in tickets_data:
        ticket = ticket_manager.get_ticket(ticket_data['ticket_id'])
        if ticket:
            # Get unread count using simple counter with 'support' identifier
            ticket_data['unread_count'] = read_status_manager.get_unread_count(ticket, 'support')
    
    return jsonify(tickets_data)


@app.route('/api/ticket-stats')
def api_ticket_stats():
    """Get ticket statistics"""
    stats = ticket_manager.get_ticket_stats()
    return jsonify(stats)


@app.route('/api/ticket/<ticket_id>')
def api_ticket_detail(ticket_id):
    ticket = ticket_manager.get_ticket(ticket_id)
    if ticket:
        ticket_data = ticket.to_dict()
        # Optionally add unread count if user_identifier provided
        user_identifier = request.args.get('user_identifier')
        if user_identifier:
            ticket_data['unread_count'] = read_status_manager.get_unread_count(ticket, user_identifier)
        return jsonify(ticket_data)
    return jsonify({'error': 'Ticket not found'}), 404


@app.route('/api/tickets/search')
def api_search_tickets():
    """Search tickets by user_id or user_name"""
    user_id = request.args.get('user_id')
    user_name = request.args.get('user_name')
    
    if user_id:
        tickets = ticket_manager.get_tickets_by_user_id(user_id)
        return jsonify([t.to_dict() for t in tickets])
    elif user_name:
        tickets = ticket_manager.get_user_tickets(user_name)
        return jsonify([t.to_dict() for t in tickets])
    
    return jsonify({'error': 'Please provide user_id or user_name'}), 400


@app.route('/api/mark-read', methods=['POST'])
def api_mark_read():
    """Mark a ticket as read - reset unread count"""
    data = request.get_json()
    ticket_id = data.get('ticket_id')
    user_identifier = data.get('user_identifier')  # email or username
    
    if not ticket_id:
        return jsonify({'error': 'ticket_id required'}), 400
    
    # For support, always use 'support' identifier
    # For users, use their email/identifier
    if user_identifier and '@' not in user_identifier:
        # Support agent - use 'support' identifier
        read_status_manager.reset_unread_count(ticket_id, 'support')
    elif user_identifier:
        # User - use their identifier
        read_status_manager.reset_unread_count(ticket_id, user_identifier)
    else:
        # Default to 'support' if no identifier provided
        read_status_manager.reset_unread_count(ticket_id, 'support')
    
    return jsonify({'success': True, 'message': f'{ticket_id} marked as read'})


@app.route('/api/unread-counts', methods=['POST'])
def api_unread_counts():
    """Get unread counts for multiple tickets"""
    data = request.get_json()
    user_identifier = data.get('user_identifier')
    ticket_ids = data.get('ticket_ids', [])
    
    if not user_identifier:
        return jsonify({'error': 'user_identifier required'}), 400
    
    unread_counts = {}
    for ticket_id in ticket_ids:
        ticket = ticket_manager.get_ticket(ticket_id)
        if ticket:
            unread_counts[ticket_id] = read_status_manager.get_unread_count(ticket, user_identifier)
    
    return jsonify(unread_counts)


# ==================== SOCKET.IO EVENTS ====================

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    
    # Get session data from Redis
    session_data = session_manager.get_session(sid)
    if session_data:
        username = session_data.get('username')
        role = session_data.get('role')
        
        # Delete session from Redis
        session_manager.delete_session(sid)
        
        # Update support person status if needed
        # if role == 'support' and support_person['sid'] == sid:
        support_person['sid'] = None
        support_person['online'] = False

        print(f"Client disconnected: {sid} (User: {username})")
    else:
        print(f"Client disconnected: {sid} (No session data)")


@socketio.on('user_join')
def handle_user_join(data):
    username = data.get('username')
    sid = request.sid
    
    # Create session in Redis
    session_manager.create_session(sid, username, 'user')
    print(f"User joined: {username} (Session: {sid})")


@socketio.on('support_join')
def handle_support_join(data):
    username = data.get('username')
    sid = request.sid
    
    # Create session in Redis
    session_manager.create_session(sid, username, 'support')
    
    # Update support person status
    support_person['name'] = username
    support_person['sid'] = sid
    support_person['online'] = True
    
    print(f"Support person joined: {username} (Session: {sid})")


@socketio.on('create_ticket')
def handle_create_ticket(data):
    # Validate session
    if not validate_session():
        emit('error', {'message': 'Session expired. Please refresh the page.'})
        return
        
    user_name = data.get('user_name')
    user_id = data.get('user_id')  # MongoDB ObjectId or user ID
    subject = data.get('subject')
    description = data.get('description')
    priority = data.get('priority', 'MEDIUM')

    # Rate Limiting: Check if user is creating tickets too quickly
    rate_limit_key = f"rate_limit:create_ticket:{user_name}"
    current_time = datetime.datetime.now().timestamp()
    
    # Get last ticket creation time
    last_creation_time = redis_client.get(rate_limit_key)
    if last_creation_time:
        time_diff = current_time - float(last_creation_time)
        cooldown_period = 5  # 5 seconds cooldown
        
        if time_diff < cooldown_period:
            remaining_time = int(cooldown_period - time_diff)
            emit('error', {'message': f'⚠️ Please wait {remaining_time} more second(s) before creating another ticket.'})
            print(f"❌ Rate limit: User {user_name} tried to create ticket too quickly (within {time_diff:.1f}s)")
            return
    
    # Set new rate limit timestamp (expires in 10 seconds)
    redis_client.setex(rate_limit_key, 10, current_time)

    # Check if user already has a pending ticket (Open or In Progress)
    user_tickets = ticket_manager.get_user_tickets(user_name)
    pending_tickets = [t for t in user_tickets if t.status in ['Open', 'In Progress']]
    
    if pending_tickets:
        emit('error', {'message': '⚠️ You already have a pending ticket. Please wait for it to be resolved before creating a new one.'})
        print(f"❌ User {user_name} tried to create ticket but has pending ticket: {pending_tickets[0].ticket_id}")
        return

    # Create ticket in Redis (persistent storage)
    ticket = ticket_manager.create_ticket(user_name, subject, description, priority, user_id)
    ticket_queue.append(ticket.ticket_id)

    # Add the initial description as the first message
    initial_message = ticket.add_message(user_name, description, 'user')
    ticket_manager.update_ticket(ticket)
    
    # Set initial unread count for support (user sent first message)
    read_status_manager.increment_unread_count(ticket.ticket_id, 'support')
    
    print(f"📝 Added initial message to ticket {ticket.ticket_id}: {description[:50]}...")
    
    # Add ticket_id to the message for frontend
    initial_message['ticket_id'] = ticket.ticket_id

    # Prepare ticket data for sending
    ticket_data = ticket.to_dict()
    print(f"📤 Sending ticket_created event with {len(ticket_data.get('messages', []))} messages")
    
    # Send to the current client who created the ticket (most reliable)
    emit('ticket_created', ticket_data)
    
    # Also send to stored user session (for multi-device support)
    user_sid = session_manager.get_user_sid(user_name)
    if user_sid and user_sid != request.sid:
        socketio.emit('ticket_created', ticket_data, room=user_sid)
    
    # Broadcast the initial message to the ticket room
    # This ensures anyone who opens the chat sees it immediately
    socketio.emit('new_message', initial_message, room=ticket.ticket_id)

    # Notify support person with updated ticket data (old method - single support)
    notify_support_person('new_ticket', ticket.to_dict())
    
    # Broadcast to ALL connected clients (support staff will see it)
    # When no room is specified, it broadcasts to all clients
    socketio.emit('new_ticket', ticket.to_dict())

    print(f"✅ Ticket created in Redis: {ticket.ticket_id} by {user_name} with initial message (Persistent)")


@socketio.on('join_room')
def handle_join_room(data):
    room = data.get('room')
    join_room(room)
    session_data = session_manager.get_session(request.sid)
    username = session_data.get('username', 'Unknown') if session_data else 'Unknown'
    
    # Mark ticket as read when user joins the room (they opened the ticket)
    ticket_id = room  # Room ID is usually the ticket ID
    if ticket_id and ticket_id.startswith('TKT-'):
        ticket = ticket_manager.get_ticket(ticket_id)
        if ticket:
            # Get user role to determine identifier
            role = session_data.get('role', 'user') if session_data else 'user'
            if role in ['admin', 'support']:
                # Support/admin opened ticket → reset unread count for 'support'
                read_status_manager.reset_unread_count(ticket_id, 'support')
            else:
                # User opened ticket → reset unread count for user
                user_identifier = session_data.get('email') or username
                read_status_manager.reset_unread_count(ticket_id, user_identifier)
            
            # Notify that ticket was marked as read
            emit('ticket_marked_read', {
                'ticket_id': ticket_id,
                'message': 'Ticket marked as read'
            })
    
    print(f"✅ Client {request.sid} ({username}) joined room {room}")


@socketio.on('leave_room')
def handle_leave_room(data):
    room = data.get('room')
    leave_room(room)
    print(f"Client {request.sid} left room {room}")


@socketio.on('send_message')
def handle_send_message(data):
    # Validate session
    if not validate_session():
        emit('error', {'message': 'Session expired. Please refresh the page.'})
        return
        
    ticket_id = data.get('ticket_id')
    sender = data.get('sender')
    message = data.get('message')
    sender_type = data.get('sender_type', 'user')

    # Get ticket from Redis
    ticket = ticket_manager.get_ticket(ticket_id)
    if ticket:
        # Auto-reopen ticket if user sends message on resolved/closed ticket
        ticket_reopened = False
        if sender_type == 'user' and ticket.status in ['Resolved', 'Closed']:
            old_status = ticket.status
            ticket.update_status('Open')
            ticket_reopened = True
            print(f"🔄 Ticket {ticket_id} auto-reopened by user message (was: {old_status})")
        
        msg = ticket.add_message(sender, message, sender_type)

        # Update ticket in Redis
        ticket_manager.update_ticket(ticket)
        
        # Add ticket_id to the message for frontend
        msg['ticket_id'] = ticket_id

        print(f"Broadcasting message to room {ticket_id}: {msg}")
        
        # Broadcast to room (including sender)
        emit('new_message', msg, room=ticket_id, include_self=True)

        # Simple unread tracking:
        # - If user sends message → increment unread count for ALL support agents
        # - If support sends message → increment unread count for user
        # - Mark sender's own message as read (reset their counter)
        session_data = session_manager.get_session(request.sid)
        if session_data:
            if sender_type == 'user':
                # User sent message → increment unread for support
                # For simplicity, increment for a generic 'support' key
                # All support agents will see the same count
                # (In production, you might want per-agent tracking)
                read_status_manager.increment_unread_count(ticket_id, 'support')
                # Mark as read for the user (they saw their own message)
                user_identifier = session_data.get('email') or sender
                read_status_manager.reset_unread_count(ticket_id, user_identifier)
            else:  # support
                # Support sent message → increment unread for user
                # Use user_name (which is the email) as identifier to match user side
                user_identifier = ticket.user_name  # User's email/name (this is the email)
                read_status_manager.increment_unread_count(ticket_id, user_identifier)
                # Mark as read for support (they saw their own message)
                # Use 'support' identifier consistently (not username)
                read_status_manager.reset_unread_count(ticket_id, 'support')
                print(f"📬 Support sent message to {ticket_id}, incremented unread for user: {user_identifier}")
        
        # Broadcast ticket list update to ALL connected clients (for unread counts)
        socketio.emit('ticket_list_update', {
            'ticket_id': ticket_id,
            'message': 'New message received'
        })

        # If ticket was reopened, notify everyone
        if ticket_reopened:
            socketio.emit('ticket_updated', {
                'ticket_id': ticket_id,
                'status': 'Open',
                'reopened': True,
                'message': 'Ticket reopened by user message'
            }, room=ticket_id)
            
            # Notify support person
            notify_support_person('ticket_reopened', {
                'ticket_id': ticket_id,
                'user_name': sender,
                'message': f'Ticket {ticket_id} was reopened by user message'
            })

        # Notify support person if message is from user
        if sender_type == 'user':
            notify_support_person('new_user_message', {
                'ticket_id': ticket_id,
                'user_name': sender,
                'message': message
            })

        print(f"✅ Message sent in {ticket_id} by {sender} ({sender_type}) - Saved to Redis")
    else:
        print(f"❌ Ticket {ticket_id} not found!")
        emit('error', {'message': 'Ticket not found'})


@socketio.on('update_ticket_status')
def handle_update_status(data):
    # Validate session
    if not validate_session():
        emit('error', {'message': 'Session expired. Please refresh the page.'})
        return
        
    ticket_id = data.get('ticket_id')
    new_status = data.get('status')

    # Get ticket from Redis
    ticket = ticket_manager.get_ticket(ticket_id)
    if ticket:
        ticket.update_status(new_status)

        # Update in Redis
        ticket_manager.update_ticket(ticket)

        # Notify user via Redis session
        user_sid = session_manager.get_user_sid(ticket.user_name)
        if user_sid:
            emit('ticket_updated', {
                'ticket_id': ticket_id,
                'status': new_status
            }, room=user_sid)

        # Broadcast to all in support
        emit('ticket_updated', {
            'ticket_id': ticket_id,
            'status': new_status
        }, room=ticket_id)

        print(f"✅ Ticket {ticket_id} status updated to {new_status} - Saved to Redis")


UPLOAD_DIR = "/tmp/uploads"  # /tmp is mandatory on Cloud Run
os.makedirs(UPLOAD_DIR, exist_ok=True)
BUCKET_NAME = os.environ.get("BUCKET_NAME", 'gameex-images')
private_key = os.environ.get("private_key",'')
client_email = os.environ.get('client_email', '')
project_id = os.environ.get('project_id', '')
from google.cloud import storage
from uuid import uuid4
import uuid


from google.oauth2 import service_account

def upload_file_to_gcs_from_path(file_path: str, filename: str) -> str:
    credentials = service_account.Credentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": project_id,
            "client_email": client_email,
            "private_key": private_key.replace("\\n", "\n"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    # client = storage.Client(credentials={"client_email":client_email, "private_key":private_key,"project_id":project_id})
    client = storage.Client(credentials=credentials)
    bucket = client.bucket(BUCKET_NAME)

    unique_name = f"{uuid.uuid4()}-{filename}"
    blob = bucket.blob(unique_name)

    blob.upload_from_filename(
        file_path,
        content_type="image/jpeg"  # don't be lazy, detect this
    )

    blob.cache_control = "public, max-age=2592000, immutable"
    blob.patch()

    return f"https://storage.googleapis.com/{BUCKET_NAME}/{unique_name}"

@app.route("/upload_image", methods=["POST"])
def upload():
    print("-----file coming",request.files)
    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400

    files = request.files.getlist("files")
    if not files or files[0].filename == '':
        return jsonify({"error": "No file selected"}), 400

    # Process first file (single file upload)
    f = files[0]
    local_path = os.path.join(UPLOAD_DIR, f.filename)
    f.save(local_path)

    try:
        # Upload to GCP and get the real URL
        gcp_url = upload_file_to_gcs_from_path(local_path, f.filename)
        os.remove(local_path)  # Clean up local file
        
        # Generate a unique file ID
        file_id = str(uuid.uuid4())
        
        # Store GCP URL in Redis with file_id as key (expires in 1 year)
        file_key = f"support_system:file:{file_id}"
        redis_client.setex(file_key, 31536000, gcp_url)  # 1 year TTL
        
        # Return proxy URL instead of GCP URL
        proxy_url = f"/file/{file_id}"
        return jsonify({"urls": proxy_url})
    except Exception as e:
        # Clean up on error
        if os.path.exists(local_path):
            os.remove(local_path)
        return jsonify({"error": str(e)}), 500

@app.route("/file/<file_id>")
def serve_file(file_id):
    """Proxy endpoint to serve files from GCP without exposing the URL"""
    try:
        # Get GCP URL from Redis
        file_key = f"support_system:file:{file_id}"
        gcp_url = redis_client.get(file_key)
        
        if not gcp_url:
            return jsonify({"error": "File not found"}), 404
        
        # Decode if bytes
        if isinstance(gcp_url, bytes):
            gcp_url = gcp_url.decode()
        
        # Fetch file from GCP
        response = requests.get(gcp_url, stream=True, timeout=30)
        
        if response.status_code != 200:
            return jsonify({"error": "Failed to fetch file"}), 500
        
        # Determine content type from response or URL
        content_type = response.headers.get('Content-Type', 'application/octet-stream')
        
        # Return file with proper headers
        return Response(
            response.iter_content(chunk_size=8192),
            content_type=content_type,
            headers={
                'Cache-Control': 'public, max-age=2592000',
                'Content-Disposition': f'inline; filename="{file_id}"'
            }
        )
    except Exception as e:
        print(f"Error serving file {file_id}: {e}")
        return jsonify({"error": "Failed to serve file"}), 500
# ==================== SESSION VALIDATION MIDDLEWARE ====================

def validate_session():
    """Validate session before processing requests"""
    sid = request.sid
    if sid:
        session_data = session_manager.get_session(sid)
        if not session_data:
            return False
    return True

# ==================== PERIODIC CLEANUP ====================

import threading
import time

def periodic_cleanup():
    """Run periodic cleanup of expired sessions - Optimized interval"""
    while True:
        try:
            session_manager.cleanup_expired_sessions()
            time.sleep(900)  # Run every 15 minutes (reduced load, Redis TTL handles expiration)
        except Exception as e:
            print(f"Cleanup error: {e}")
            time.sleep(60)  # Wait 1 minute on error

# Start cleanup thread
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

# ==================== RUN APP ====================

if __name__ == '__main__':
    print("=" * 60)
    print("🎫 SUPPORT TICKET SYSTEM - Flask SocketIO with MongoDB Auth")
    print("=" * 60)
    
    # Initialize default admin if none exists
    init_default_admin()
    
    print("\nFeatures:")
    print("  ✅ MongoDB User Authentication")
    print("  ✅ Admin/Support Login System")
    print("  ✅ Persistent Ticket Storage in Redis")
    print("  ✅ Tickets survive server restarts")
    print("  ✅ Redis Session Management")
    print("  ✅ Session Limitations (3 sessions per user)")
    print("  ✅ Session Timeout (1 hour)")
    print("  ✅ Automatic Cleanup")
    print("  ✅ Real-time Chat")
    print("=" * 60)
    print("📦 Storage:")
    print("  • Users: MongoDB")
    print("  • Sessions: Redis (1 hour TTL)")
    print("  • Tickets: Redis (Permanent until deleted)")
    print("  • Messages: Redis (with tickets)")
    print("=" * 60)
    print("🔐 Admin Management:")
    print("  • Run: python create_admin.py")
    print("  • Create admins, support users, and test users")
    print("=" * 60)
    print("🚀 Starting server on http://localhost:5200")
    print("=" * 60)
    socketio.run(app, debug=False, host='0.0.0.0', port=5010,allow_unsafe_werkzeug=True  # <-- add this //5010
)