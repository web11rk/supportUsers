from flask import Flask, render_template, request, redirect, url_for, session, jsonify, render_template_string, make_response, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os
import datetime
import json
from collections import deque
from enum import Enum
import redis
from flask_session import Session
from functools import wraps
from auth import Auth, init_default_admin
from jwt_auth import JWTAuth, jwt_required, jwt_admin_required
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

DEFAULT_TICKET_PAGE_SIZE = 25
MAX_TICKET_PAGE_SIZE = 100
DEFAULT_MESSAGE_PAGE_SIZE = 30
MAX_MESSAGE_PAGE_SIZE = 100


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

        # Sorted sets scored by updated_at timestamp → O(log N) paged reads
        self.active_sorted_key = "support_system:tickets:active:sorted"
        self.resolved_sorted_key = "support_system:tickets:resolved:sorted"
        self.all_sorted_key = "support_system:tickets:all:sorted"

        # TTL for resolved tickets (optional - 0 means keep forever)
        self.resolved_ticket_ttl = int(os.getenv('RESOLVED_TICKET_TTL', 2592000))  # 30 days default
        
        # Initialize counter from Redis or start at 1000
        if not self.redis.exists(self.ticket_counter_key):
            self.redis.set(self.ticket_counter_key, 1000)

        self.summary_fields = [
            'ticket_id', 'user_name', 'user_id', 'subject', 'description',
            'priority', 'status', 'created_at', 'updated_at', 'assigned_to', 'room_id',
            'message_count', 'last_message', 'last_message_at', 'last_message_sender', 'last_message_type'
        ]
    
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
            'room_id': ticket_id,
            'message_count': '0',
            'last_message': '',
            'last_message_at': datetime.datetime.now().isoformat(),
            'last_message_sender': user_name,
            'last_message_type': 'user'
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

        # Add to sorted sets for time-ordered pagination
        ts = datetime.datetime.now().timestamp()
        self.redis.zadd(self.all_sorted_key, {ticket_id: ts})
        self.redis.zadd(self.active_sorted_key, {ticket_id: ts})

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
        ticket_dict['messages'] = json.loads(ticket_dict.get('messages', '[]'))
        
        return Ticket.from_dict(ticket_dict)
    
    def update_ticket(self, ticket):
        """Update a ticket in Redis and manage status indexes"""
        ticket_dict = ticket.to_dict()
        
        # Convert messages to JSON string
        redis_data = ticket_dict.copy()
        redis_data['messages'] = json.dumps(ticket_dict['messages'])
        redis_data['assigned_to'] = redis_data['assigned_to'] or ''
        redis_data['message_count'] = str(len(ticket_dict['messages']))

        if ticket_dict['messages']:
            last_msg = ticket_dict['messages'][-1]
            redis_data['last_message'] = last_msg.get('message', '')
            redis_data['last_message_at'] = last_msg.get('timestamp', ticket.updated_at)
            redis_data['last_message_sender'] = last_msg.get('sender', ticket.user_name)
            redis_data['last_message_type'] = last_msg.get('sender_type', 'user')
        else:
            redis_data['last_message'] = ticket.description
            redis_data['last_message_at'] = ticket.updated_at
            redis_data['last_message_sender'] = ticket.user_name
            redis_data['last_message_type'] = 'user'
        
        # Store and index updates in one batch (fewer network round-trips)
        status = ticket.status
        ticket_key = f"{self.ticket_prefix}{ticket.ticket_id}"
        pipe = self.redis.pipeline(transaction=False)

        pipe.hset(ticket_key, mapping=redis_data)

        # Sorted set score = updated_at timestamp
        try:
            ts = datetime.datetime.fromisoformat(ticket.updated_at).timestamp()
        except (TypeError, ValueError):
            ts = datetime.datetime.now().timestamp()

        pipe.zadd(self.all_sorted_key, {ticket.ticket_id: ts})

        # Update status indexes based on current status
        if status in ['Open', 'In Progress']:
            pipe.sadd(self.active_tickets_key, ticket.ticket_id)
            pipe.srem(self.resolved_tickets_key, ticket.ticket_id)
            pipe.zadd(self.active_sorted_key, {ticket.ticket_id: ts})
            pipe.zrem(self.resolved_sorted_key, ticket.ticket_id)
        elif status in ['Resolved', 'Closed']:
            pipe.srem(self.active_tickets_key, ticket.ticket_id)
            pipe.sadd(self.resolved_tickets_key, ticket.ticket_id)
            pipe.zrem(self.active_sorted_key, ticket.ticket_id)
            pipe.zadd(self.resolved_sorted_key, {ticket.ticket_id: ts})

            # Set TTL on resolved tickets (optional - 0 means keep forever)
            if self.resolved_ticket_ttl > 0:
                pipe.expire(ticket_key, self.resolved_ticket_ttl)

        pipe.execute()
    
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

    def _decode_summary_row(self, row):
        if not row:
            return None

        data = {}
        for i, field in enumerate(self.summary_fields):
            value = row[i] if i < len(row) else None
            if isinstance(value, bytes):
                value = value.decode()
            data[field] = value

        if not data.get('ticket_id'):
            return None

        try:
            data['message_count'] = int(data.get('message_count') or 0)
        except (TypeError, ValueError):
            data['message_count'] = 0

        data['user_id'] = data.get('user_id') or ''
        data['assigned_to'] = data.get('assigned_to') or ''
        data['last_message'] = data.get('last_message') or data.get('description') or ''
        data['last_message_at'] = data.get('last_message_at') or data.get('updated_at')
        data['last_message_sender'] = data.get('last_message_sender') or data.get('user_name')
        data['last_message_type'] = data.get('last_message_type') or 'user'

        return data

    def get_ticket_summary(self, ticket_id):
        key = f"{self.ticket_prefix}{ticket_id}"
        row = self.redis.hmget(key, self.summary_fields)
        return self._decode_summary_row(row)

    def _get_summaries_from_ids(self, ticket_ids):
        ids = [tid.decode() if isinstance(tid, bytes) else tid for tid in ticket_ids]
        if not ids:
            return []
        pipe = self.redis.pipeline(transaction=False)
        for tid in ids:
            pipe.hmget(f"{self.ticket_prefix}{tid}", self.summary_fields)

        rows = pipe.execute()
        summaries = []
        for row in rows:
            decoded = self._decode_summary_row(row)
            if decoded:
                summaries.append(decoded)
        return summaries

    def get_active_ticket_summaries(self):
        ticket_ids = self.redis.smembers(self.active_tickets_key)
        return self._get_summaries_from_ids(ticket_ids)

    def get_resolved_ticket_summaries(self):
        ticket_ids = self.redis.smembers(self.resolved_tickets_key)
        return self._get_summaries_from_ids(ticket_ids)

    def get_all_ticket_summaries(self):
        ticket_ids = self.redis.smembers(self.ticket_list_key)
        return self._get_summaries_from_ids(ticket_ids)

    def get_user_ticket_summaries(self, user_name):
        ticket_ids = self.redis.smembers(f"{self.user_tickets_prefix}{user_name}")
        return self._get_summaries_from_ids(ticket_ids)
    
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

    def get_ticket_messages_page(self, ticket_id, limit=DEFAULT_MESSAGE_PAGE_SIZE, before=None):
        """Get one page of messages for a ticket.

        Messages are returned in normal chronological order.
        `before` represents the end index boundary for older-message loading.
        """
        ticket = self.get_ticket(ticket_id)
        if not ticket:
            return None, None

        messages = ticket.messages or []
        total_messages = len(messages)

        limit = max(1, min(limit, MAX_MESSAGE_PAGE_SIZE))

        if before is None:
            end_index = total_messages
        else:
            end_index = max(0, min(before, total_messages))

        start_index = max(0, end_index - limit)
        page_messages = messages[start_index:end_index]

        return ticket, {
            'messages': page_messages,
            'message_count': total_messages,
            'messages_loaded': len(page_messages),
            'has_more_messages': start_index > 0,
            'next_before': start_index if start_index > 0 else None
        }

    def delete_ticket(self, ticket_id):
        """Delete ticket and related indexes/counters from Redis."""
        ticket_key = f"{self.ticket_prefix}{ticket_id}"
        if not self.redis.exists(ticket_key):
            return False

        # Fetch minimal owner fields for index cleanup
        user_name, user_id = self.redis.hmget(ticket_key, ['user_name', 'user_id'])
        if isinstance(user_name, bytes):
            user_name = user_name.decode()
        if isinstance(user_id, bytes):
            user_id = user_id.decode()

        pipe = self.redis.pipeline(transaction=False)

        # Remove main ticket data + status/global indexes
        pipe.delete(ticket_key)
        pipe.srem(self.ticket_list_key, ticket_id)
        pipe.srem(self.active_tickets_key, ticket_id)
        pipe.srem(self.resolved_tickets_key, ticket_id)
        pipe.zrem(self.all_sorted_key, ticket_id)
        pipe.zrem(self.active_sorted_key, ticket_id)
        pipe.zrem(self.resolved_sorted_key, ticket_id)

        # Remove user indexes
        if user_name:
            pipe.srem(f"{self.user_tickets_prefix}{user_name}", ticket_id)
        if user_id:
            pipe.srem(f"{self.user_tickets_prefix}uid:{user_id}", ticket_id)

        # Remove unread counters (support + user variants)
        unread_pattern = f"support_system:unread_count:{ticket_id}:*"
        for key in self.redis.scan_iter(match=unread_pattern, count=20):
            pipe.delete(key)

        pipe.execute()
        return True

    def get_paged_summaries_from_sorted_set(self, sorted_key, offset, limit):
        """Fetch a time-ordered page of summaries via ZREVRANGE.

        O(log N + M) instead of O(N) — only the requested page is loaded.
        Returns (summaries_list, total_count).
        """
        pipe = self.redis.pipeline(transaction=False)
        pipe.zrevrange(sorted_key, offset, offset + limit - 1)
        pipe.zcard(sorted_key)
        ids_raw, total = pipe.execute()

        ids = [tid.decode() if isinstance(tid, bytes) else tid for tid in ids_raw]
        if not ids:
            return [], int(total)

        pipe2 = self.redis.pipeline(transaction=False)
        for tid in ids:
            pipe2.hmget(f"{self.ticket_prefix}{tid}", self.summary_fields)
        rows = pipe2.execute()

        summaries = []
        for row in rows:
            decoded = self._decode_summary_row(row)
            if decoded:
                summaries.append(decoded)
        return summaries, int(total)

    def migrate_to_sorted_sets(self):
        """One-time migration: populate sorted sets from existing plain SET indexes.

        Idempotent — safe to call on every startup, skips if already populated.
        """
        if self.redis.zcard(self.all_sorted_key) > 0:
            return  # Already migrated

        all_ids = self.redis.smembers(self.ticket_list_key)
        if not all_ids:
            return

        ids = [tid.decode() if isinstance(tid, bytes) else tid for tid in all_ids]

        # Batch-fetch updated_at + status for every ticket
        pipe = self.redis.pipeline(transaction=False)
        for tid in ids:
            pipe.hmget(f"{self.ticket_prefix}{tid}", ['updated_at', 'status'])
        rows = pipe.execute()

        all_scores = {}
        active_scores = {}
        resolved_scores = {}

        for i, row in enumerate(rows):
            tid = ids[i]
            updated_at_raw = row[0].decode() if isinstance(row[0], bytes) else row[0]
            status_raw = row[1].decode() if isinstance(row[1], bytes) else row[1]
            if not updated_at_raw:
                continue
            try:
                ts = datetime.datetime.fromisoformat(updated_at_raw).timestamp()
            except (TypeError, ValueError):
                continue
            all_scores[tid] = ts
            if status_raw in ['Open', 'In Progress']:
                active_scores[tid] = ts
            elif status_raw in ['Resolved', 'Closed']:
                resolved_scores[tid] = ts

        pipe2 = self.redis.pipeline(transaction=False)
        if all_scores:
            pipe2.zadd(self.all_sorted_key, all_scores)
        if active_scores:
            pipe2.zadd(self.active_sorted_key, active_scores)
        if resolved_scores:
            pipe2.zadd(self.resolved_sorted_key, resolved_scores)
        pipe2.execute()
        print(f"✅ Migrated {len(all_scores)} tickets to sorted sets")


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
        return self.get_unread_count_by_ticket_id(ticket.ticket_id, user_identifier)

    def get_unread_count_by_ticket_id(self, ticket_id, user_identifier):
        """Get unread count using ticket_id directly (faster for list APIs)."""
        key = f"{self.unread_count_prefix}{ticket_id}:{user_identifier}"
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

session_manager = RedisSessionManager(redis_client)
ticket_manager = RedisTicketManager(redis_client)
read_status_manager = RedisReadStatusManager(redis_client)

# Populate sorted sets from existing tickets (idempotent — skips if already done)
ticket_manager.migrate_to_sorted_sets()


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


def build_ticket_summary(ticket):
    """Return a lightweight summary for ticket list views."""
    if isinstance(ticket, dict):
        return ticket

    last_message = ticket.messages[-1] if ticket.messages else None
    return {
        'ticket_id': ticket.ticket_id,
        'user_name': ticket.user_name,
        'user_id': ticket.user_id,
        'subject': ticket.subject,
        'description': ticket.description,
        'priority': ticket.priority,
        'status': ticket.status,
        'created_at': ticket.created_at,
        'updated_at': ticket.updated_at,
        'assigned_to': ticket.assigned_to,
        'room_id': ticket.room_id,
        'message_count': len(ticket.messages),
        'last_message': last_message['message'] if last_message else ticket.description,
        'last_message_at': last_message['timestamp'] if last_message else ticket.updated_at,
        'last_message_sender': last_message['sender'] if last_message else ticket.user_name,
        'last_message_type': last_message['sender_type'] if last_message else 'user'
    }


def sort_tickets_for_list(tickets):
    """Sort tickets for list endpoints by `updated_at` (newest first)."""
    def read_field(item, field, default=None):
        if isinstance(item, dict):
            return item.get(field, default)
        return getattr(item, field, default)

    def read_updated_at_timestamp(item):
        raw = read_field(item, 'updated_at')
        if not raw:
            return 0
        try:
            return datetime.datetime.fromisoformat(raw).timestamp()
        except (TypeError, ValueError):
            return 0

    return sorted(tickets, key=read_updated_at_timestamp, reverse=True)


def paginate_list(items, page, limit):
    total = len(items)
    start = (page - 1) * limit
    end = start + limit
    paged_items = items[start:end]
    return paged_items, {
        'page': page,
        'limit': limit,
        'total': total,
        'has_more': end < total,
        'total_pages': (total + limit - 1) // limit if limit else 1
    }


def get_request_pagination(default_limit=DEFAULT_TICKET_PAGE_SIZE, max_limit=MAX_TICKET_PAGE_SIZE):
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except (TypeError, ValueError):
        page = 1

    try:
        limit = int(request.args.get('limit', default_limit))
    except (TypeError, ValueError):
        limit = default_limit

    limit = max(1, min(limit, max_limit))
    return page, limit


def notify_support_person(event, data):
    """Socket notifications removed (API-only mode)."""
    return


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
    include_meta = request.args.get('include_meta', 'false').lower() == 'true'
    page, limit = get_request_pagination()

    tickets = sort_tickets_for_list(ticket_manager.get_user_ticket_summaries(username))
    paged_tickets, pagination = paginate_list(tickets, page, limit)
    tickets_data = [build_ticket_summary(ticket) for ticket in paged_tickets]
    
    # Add unread counts using ticket_id directly
    for ticket_data in tickets_data:
        ticket_data['unread_count'] = read_status_manager.get_unread_count_by_ticket_id(
            ticket_data['ticket_id'], user_email
        )

    if include_meta:
        return jsonify({'tickets': tickets_data, 'pagination': pagination})

    return jsonify(tickets_data)


@app.route('/api/support/tickets')
@limiter.limit("60 per minute")
def api_support_tickets():
    support_email = request.args.get('support_email', 'support')  # kept for API compatibility
    status_filter = request.args.get('status', 'active')  # Filter: 'all', 'active', 'resolved'
    include_meta = request.args.get('include_meta', 'false').lower() == 'true'
    compact = request.args.get('compact', 'false').lower() == 'true'
    page, limit = get_request_pagination()

    # Pick sorted set by filter — ZREVRANGE gives latest-first page without loading all tickets
    if status_filter == 'active':
        sorted_key = ticket_manager.active_sorted_key
    elif status_filter == 'resolved':
        sorted_key = ticket_manager.resolved_sorted_key
    else:  # 'all' or any other value
        sorted_key = ticket_manager.all_sorted_key

    offset = (page - 1) * limit
    paged_tickets, total = ticket_manager.get_paged_summaries_from_sorted_set(sorted_key, offset, limit)

    pagination = {
        'page': page,
        'limit': limit,
        'total': total,
        'has_more': (offset + limit) < total,
        'total_pages': (total + limit - 1) // limit if limit else 1
    }

    tickets_data = [build_ticket_summary(t) for t in paged_tickets]
    
    # Add unread counts using ticket_id directly
    for ticket_data in tickets_data:
        ticket_data['unread_count'] = read_status_manager.get_unread_count_by_ticket_id(
            ticket_data['ticket_id'], 'support'
        )

    if compact:
        tickets_data = [{
            'ticket_id': t.get('ticket_id'),
            'user_name': t.get('user_name'),
            'subject': t.get('subject'),
            'status': t.get('status'),
            'created_at': t.get('created_at'),
            'updated_at': t.get('updated_at'),
            'message_count': t.get('message_count', 0),
            'last_message': t.get('last_message'),
            'last_message_at': t.get('last_message_at'),
            'unread_count': t.get('unread_count', 0)
        } for t in tickets_data]

    if include_meta:
        return jsonify({'tickets': tickets_data, 'pagination': pagination})

    return jsonify(tickets_data)


@app.route('/api/ticket-stats')
def api_ticket_stats():
    """Get ticket statistics"""
    stats = ticket_manager.get_ticket_stats()
    return jsonify(stats)


@app.route('/api/ticket/<ticket_id>')
def api_ticket_detail(ticket_id):
    start_time = time.time()
    ticket = ticket_manager.get_ticket(ticket_id)
    end_time = time.time()
    print(ticket)
    print(f"⏱️ Fetched ticket {ticket_id} in {end_time - start_time:.4f} seconds")
    if ticket:
        ticket_data = ticket.to_dict()
        return jsonify(ticket_data)
        message_limit = request.args.get('message_limit')
        before = request.args.get('before')

        if message_limit is not None:
            try:
                parsed_limit = max(1, min(int(message_limit), MAX_MESSAGE_PAGE_SIZE))
            except (TypeError, ValueError):
                parsed_limit = DEFAULT_MESSAGE_PAGE_SIZE

            try:
                before_index = int(before) if before is not None else None
            except (TypeError, ValueError):
                before_index = None

            _, message_page = ticket_manager.get_ticket_messages_page(
                ticket_id,
                limit=parsed_limit,
                before=before_index
            )

            if message_page:
                ticket_data['messages'] = message_page['messages']
                ticket_data['message_count'] = message_page['message_count']
                ticket_data['messages_loaded'] = message_page['messages_loaded']
                ticket_data['has_more_messages'] = message_page['has_more_messages']
                ticket_data['next_before'] = message_page['next_before']
        else:
            ticket_data['message_count'] = len(ticket.messages)
            ticket_data['messages_loaded'] = len(ticket.messages)
            ticket_data['has_more_messages'] = False
            ticket_data['next_before'] = None

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


# ==================== TICKET ACTION APIs (API-ONLY MODE) ====================

@app.route('/api/tickets', methods=['POST'])
@limiter.limit("20 per minute")
def api_create_ticket():
    """Create ticket via HTTP (Socket removed)."""
    data = request.get_json(silent=True) or {}

    user_name = data.get('user_name')
    user_id = data.get('user_id')
    subject = data.get('subject')
    description = data.get('description')
    priority = data.get('priority', 'MEDIUM')

    if not user_name or not subject or not description:
        return jsonify({'error': 'user_name, subject and description are required'}), 400

    rate_limit_key = f"rate_limit:create_ticket:{user_name}"
    current_time = datetime.datetime.now().timestamp()
    last_creation_time = redis_client.get(rate_limit_key)
    if last_creation_time:
        time_diff = current_time - float(last_creation_time)
        cooldown_period = 5
        if time_diff < cooldown_period:
            remaining_time = int(cooldown_period - time_diff)
            return jsonify({'error': f'⚠️ Please wait {remaining_time} more second(s) before creating another ticket.'}), 429

    redis_client.setex(rate_limit_key, 10, current_time)

    user_tickets = ticket_manager.get_user_tickets(user_name)
    pending_tickets = [t for t in user_tickets if t.status in ['Open', 'In Progress']]
    if pending_tickets:
        return jsonify({'error': '⚠️ You already have a pending ticket. Please wait for it to be resolved before creating a new one.'}), 400

    ticket = ticket_manager.create_ticket(user_name, subject, description, priority, user_id)
    ticket_queue.append(ticket.ticket_id)

    initial_message = ticket.add_message(user_name, description, 'user')
    ticket_manager.update_ticket(ticket)
    read_status_manager.increment_unread_count(ticket.ticket_id, 'support')

    initial_message['ticket_id'] = ticket.ticket_id
    ticket_data = ticket.to_dict()
    ticket_data['initial_message'] = initial_message

    return jsonify({'success': True, 'ticket': ticket_data}), 201


@app.route('/api/ticket/<ticket_id>/message', methods=['POST'])
@limiter.limit("120 per minute")
def api_send_message(ticket_id):
    """Send message via HTTP (Socket removed)."""
    data = request.get_json(silent=True) or {}

    sender = data.get('sender')
    message = data.get('message')
    sender_type = data.get('sender_type', 'user')

    if not sender or not message:
        return jsonify({'error': 'sender and message are required'}), 400

    ticket = ticket_manager.get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    ticket_reopened = False
    if sender_type == 'user' and ticket.status in ['Resolved', 'Closed']:
        ticket.update_status('Open')
        ticket_reopened = True

    msg = ticket.add_message(sender, message, sender_type)
    ticket_manager.update_ticket(ticket)
    msg['ticket_id'] = ticket_id

    if sender_type == 'user':
        read_status_manager.increment_unread_count(ticket_id, 'support')
        read_status_manager.reset_unread_count(ticket_id, sender)
    else:
        user_identifier = ticket.user_name
        read_status_manager.increment_unread_count(ticket_id, user_identifier)
        read_status_manager.reset_unread_count(ticket_id, 'support')

    return jsonify({
        'success': True,
        'message': msg,
        'ticket_id': ticket_id,
        'status': ticket.status,
        'reopened': ticket_reopened
    })


@app.route('/api/ticket/<ticket_id>/status', methods=['POST'])
@limiter.limit("60 per minute")
def api_update_ticket_status(ticket_id):
    """Update ticket status via HTTP (Socket removed)."""
    data = request.get_json(silent=True) or {}
    new_status = data.get('status')

    if not new_status:
        return jsonify({'error': 'status is required'}), 400

    ticket = ticket_manager.get_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    ticket.update_status(new_status)
    ticket_manager.update_ticket(ticket)

    return jsonify({'success': True, 'ticket_id': ticket_id, 'status': new_status})


@app.route('/api/ticket/<ticket_id>', methods=['DELETE'])
@admin_required
@limiter.limit("30 per minute")
def api_delete_ticket(ticket_id):
    """Delete ticket (admin/support only)."""
    deleted = ticket_manager.delete_ticket(ticket_id)
    if not deleted:
        return jsonify({'error': 'Ticket not found'}), 404

    # Best effort: remove from in-memory queue if present
    try:
        if ticket_id in ticket_queue:
            ticket_queue.remove(ticket_id)
    except ValueError:
        pass

    return jsonify({'success': True, 'ticket_id': ticket_id})


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
    """HTTP mode: validate session context if needed."""
    # Socket sessions removed; keep as compatible no-op for existing checks.
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
    
    app.run(debug=False, host='0.0.0.0', port=5010)