# PythonAnywhere Deployment Guide

This guide will help you deploy your Activity Tracker app to PythonAnywhere.

## Prerequisites

1. A PythonAnywhere account (free or paid)
2. Your Strava API credentials
3. Your Google Sheets service account credentials (`gcloud-creds.json`)

## Step 1: Upload Your Code

1. **Option A: Using Git (Recommended)**
   ```bash
   # In PythonAnywhere console
   git clone https://github.com/yourusername/MyActivityTracker.git
   cd MyActivityTracker
   ```

2. **Option B: Upload files manually**
   - Use the Files tab in PythonAnywhere dashboard
   - Upload all your files to `/home/yourusername/MyActivityTracker/`

## Step 2: Set Up Virtual Environment

In the PythonAnywhere console:

```bash
cd /home/yourusername/MyActivityTracker
python3.10 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Step 3: Configure Environment Variables

1. Create a `.env` file based on `env.example`:
   ```bash
   cp env.example .env
   nano .env  # or use the Files tab to edit
   ```

2. Update the `.env` file with your actual values:
   ```
   SECRET_KEY=your-super-secret-key-here-make-it-long-and-random
   FLASK_ENV=production
   
   STRAVA_CLIENT_ID=your-strava-client-id
   STRAVA_CLIENT_SECRET=your-strava-client-secret
   STRAVA_REDIRECT_URI=https://yourusername.pythonanywhere.com/callback
   
   GOOGLE_CREDS_FILE=gcloud-creds.json
   DB_PATH=activity_tracker.db
   
   SHEET_NAME=My Activities
   SHEET_ID=your-google-sheets-id
   ```

## Step 4: Update Strava App Settings

1. Go to https://www.strava.com/settings/api
2. Update your Authorization Callback Domain to: `yourusername.pythonanywhere.com`
3. Update the Authorization Callback URL to: `https://yourusername.pythonanywhere.com/callback`

## Step 5: Set Up Web App in PythonAnywhere

1. Go to the **Web** tab in your PythonAnywhere dashboard
2. Click **"Add a new web app"**
3. Choose **"Manual configuration"**
4. Select **Python 3.10** (or your preferred version)
5. Click **Next**

## Step 6: Configure WSGI File

1. In the **Web** tab, find the **"Code"** section
2. Click on the WSGI configuration file link
3. Replace the contents with the code from your `wsgi.py` file
4. **Important**: Update the `project_home` path in `wsgi.py`:
   ```python
   project_home = '/home/yourusername/MyActivityTracker'  # Replace 'yourusername'
   ```

## Step 7: Set Up Static Files (Optional)

In the **Web** tab, scroll down to **"Static files"**:
- URL: `/static/`
- Directory: `/home/yourusername/MyActivityTracker/static/`

## Step 8: Upload Google Credentials

1. Make sure your `gcloud-creds.json` file is in your project directory
2. Verify the file permissions allow reading:
   ```bash
   chmod 644 gcloud-creds.json
   ```

## Step 9: Initialize Database

In the PythonAnywhere console:

```bash
cd /home/yourusername/MyActivityTracker
source venv/bin/activate
python3 -c "from app import init_db, migrate_db; init_db(); migrate_db(); print('Database initialized!')"
```

## Step 10: Start Your Web App

1. Go back to the **Web** tab
2. Click the **"Reload"** button
3. Your app should now be available at `https://yourusername.pythonanywhere.com`

## Troubleshooting

### Check Error Logs
- Go to **Web** tab → **Log files** section
- Check both error log and server log for issues

### Common Issues

1. **Import errors**: Make sure all packages are installed in your virtual environment
2. **Permission errors**: Check file permissions, especially for `gcloud-creds.json`
3. **Database errors**: Ensure the database file is writable
4. **Environment variables**: Verify your `.env` file is properly formatted

### Testing Your App

1. Visit your app URL
2. Try to connect to Strava
3. Check if you can manage spreadsheets
4. Test importing activities

## Security Notes

- Keep your `.env` file secure and never commit it to version control
- Use strong, unique values for `SECRET_KEY`
- Regularly rotate your API keys
- Consider using PythonAnywhere's environment variables feature for sensitive data

## Updating Your App

To update your app:

```bash
cd /home/yourusername/MyActivityTracker
git pull  # if using git
source venv/bin/activate
pip install -r requirements.txt  # if dependencies changed
# Go to Web tab and reload your app
```

## Support

- PythonAnywhere Help: https://help.pythonanywhere.com/
- Flask on PythonAnywhere: https://help.pythonanywhere.com/pages/Flask/ 