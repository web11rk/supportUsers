#!/usr/bin/env python3
"""
Setup script for the Support Ticket System
This script helps install dependencies and start Redis if needed
"""

import subprocess
import sys
import os
import time

def run_command(command, description):
    """Run a command and handle errors"""
    print(f"🔄 {description}...")
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        print(f"✅ {description} completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed: {e.stderr}")
        return False

def check_redis_running():
    """Check if Redis is running"""
    try:
        result = subprocess.run("redis-cli ping", shell=True, capture_output=True, text=True)
        return "PONG" in result.stdout
    except:
        return False

def install_dependencies():
    """Install Python dependencies"""
    print("📦 Installing Python dependencies...")
    return run_command("pip install -r requirements.txt", "Installing dependencies")

def start_redis():
    """Start Redis server if not running"""
    if check_redis_running():
        print("✅ Redis is already running")
        return True
    
    print("🚀 Starting Redis server...")
    if sys.platform == "darwin":  # macOS
        return run_command("brew services start redis", "Starting Redis with Homebrew")
    elif sys.platform == "linux":
        return run_command("sudo systemctl start redis", "Starting Redis with systemctl")
    else:
        print("⚠️  Please start Redis manually on your system")
        return False

def main():
    """Main setup function"""
    print("🎫 Support Ticket System Setup")
    print("=" * 40)
    
    # Check if we're in the right directory
    if not os.path.exists("main.py"):
        print("❌ Please run this script from the project directory")
        sys.exit(1)
    
    # Install dependencies
    if not install_dependencies():
        print("❌ Failed to install dependencies")
        sys.exit(1)
    
    # Start Redis
    if not start_redis():
        print("⚠️  Redis setup failed. Please start Redis manually:")
        print("   - macOS: brew services start redis")
        print("   - Linux: sudo systemctl start redis")
        print("   - Windows: Download and run Redis server")
    
    print("\n🎉 Setup completed!")
    print("To run the application:")
    print("   python main.py")
    print("\nMake sure Redis is running before starting the app!")

if __name__ == "__main__":
    main()
