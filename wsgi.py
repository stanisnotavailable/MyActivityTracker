#!/usr/bin/python3

"""
WSGI configuration for PythonAnywhere deployment.

This file exposes the WSGI callable as a module-level variable named ``application``.
For more information on this file, see https://help.pythonanywhere.com/pages/Flask/
"""

import sys
import os

# Add your project directory to the sys.path
project_home = '/home/stanisnotavailable/MyActivityTracker'  # Update this with your PythonAnywhere username
if project_home not in sys.path:
    sys.path = [project_home] + sys.path

# Set environment variables (you can also use a .env file)
os.environ['FLASK_ENV'] = 'production'

# Import your Flask application
from app import app as application

# Initialize the database on startup
from app import init_db, migrate_db
init_db()
migrate_db()

if __name__ == "__main__":
    application.run() 