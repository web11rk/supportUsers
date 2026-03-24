#!/usr/bin/env python3
"""
Admin User Management Script
Create and manage admin/support users in MongoDB
"""

from auth import Auth, init_default_admin
import getpass
import sys


def create_admin():
    """Interactive admin creation"""
    print("\n" + "="*50)
    print("🔐 Create Admin/Support User")
    print("="*50)
    
    email = input("Email: ").strip()
    if not email:
        print("❌ Email is required")
        return
    
    name = input("Full Name: ").strip()
    if not name:
        print("❌ Name is required")
        return
    
    print("\nRole Options:")
    print("1. Admin (Full access)")
    print("2. Support (Support dashboard access)")
    role_choice = input("Select role (1 or 2): ").strip()
    
    if role_choice == '1':
        role = 'admin'
    elif role_choice == '2':
        role = 'support'
    else:
        print("❌ Invalid role selection")
        return
    
    password = getpass.getpass("Password: ")
    if len(password) < 6:
        print("❌ Password must be at least 6 characters")
        return
    
    password_confirm = getpass.getpass("Confirm Password: ")
    if password != password_confirm:
        print("❌ Passwords do not match")
        return
    
    # Create user
    user_id, success = Auth.create_user(email, password, name, role)
    
    if success:
        print(f"\n✅ {role.title()} user created successfully!")
        print(f"   User ID: {user_id}")
        print(f"   Email: {email}")
        print(f"   Name: {name}")
        print(f"   Role: {role}")
    else:
        print(f"\n❌ Error: {user_id.get('error', 'Unknown error')}")


def list_admins():
    """List all admin and support users"""
    print("\n" + "="*50)
    print("👥 Admin & Support Users")
    print("="*50)
    
    admins = Auth.get_all_admins()
    
    if not admins:
        print("No admin or support users found.")
        return
    
    for user in admins:
        print(f"\nID: {user['_id']}")
        print(f"Name: {user['name']}")
        print(f"Email: {user['email']}")
        print(f"Role: {user['role']}")
        print(f"Active: {user['active']}")
        print(f"Created: {user['created_at']}")
        if user.get('last_login'):
            print(f"Last Login: {user['last_login']}")
        print("-" * 50)


def create_regular_user():
    """Create a regular user (for testing)"""
    print("\n" + "="*50)
    print("👤 Create Regular User")
    print("="*50)
    
    email = input("Email: ").strip()
    name = input("Full Name: ").strip()
    password = getpass.getpass("Password: ")
    
    user_id, success = Auth.create_user(email, password, name, 'user')
    
    if success:
        print(f"\n✅ User created successfully!")
        print(f"   User ID: {user_id}")
        print(f"   Email: {email}")
    else:
        print(f"\n❌ Error: {user_id.get('error', 'Unknown error')}")


def test_login():
    """Test login credentials"""
    print("\n" + "="*50)
    print("🔑 Test Login")
    print("="*50)
    
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    
    user, success = Auth.authenticate(email, password)
    
    if success:
        print("\n✅ Login successful!")
        print(f"   Name: {user['name']}")
        print(f"   Role: {user['role']}")
    else:
        print("\n❌ Login failed!")
        print(f"   {user.get('error', 'Unknown error')}")


def main_menu():
    """Main menu"""
    while True:
        print("\n" + "="*50)
        print("🎫 Support System - User Management")
        print("="*50)
        print("\n1. Create Admin User")
        print("2. Create Support User")
        print("3. Create Regular User (for testing)")
        print("4. List All Admins/Support")
        print("5. Test Login")
        print("6. Initialize Default Admin")
        print("0. Exit")
        
        choice = input("\nSelect option: ").strip()
        
        if choice == '1':
            create_admin()
        elif choice == '2':
            print("\n" + "="*50)
            print("🛠️ Create Support User")
            print("="*50)
            email = input("Email: ").strip()
            name = input("Full Name: ").strip()
            password = getpass.getpass("Password: ")
            user_id, success = Auth.create_user(email, password, name, 'support')
            if success:
                print(f"\n✅ Support user created! ID: {user_id}")
            else:
                print(f"\n❌ Error: {user_id.get('error')}")
        elif choice == '3':
            create_regular_user()
        elif choice == '4':
            list_admins()
        elif choice == '5':
            test_login()
        elif choice == '6':
            init_default_admin()
        elif choice == '0':
            print("\nGoodbye!")
            sys.exit(0)
        else:
            print("\n❌ Invalid option")


if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║   Support System - Admin User Management Tool        ║
║   MongoDB User Storage                               ║
╚══════════════════════════════════════════════════════╝
    """)
    
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Goodbye!")
        sys.exit(0)

