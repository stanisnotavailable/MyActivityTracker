# My Activity Tracker

A simple web application that imports your Strava activities into Google Sheets.

## Features

- Connect to your Strava account
- Import activities to Google Sheets
- Support for multiple spreadsheets
- Connect to spreadsheets using full Google Sheets URLs
- Persistent authentication (no need to re-authenticate unless token expires)
- Simple and clean UI
- Optional data columns - choose which activity data to import
- Per-spreadsheet column preferences - remember which columns to use for each spreadsheet

## Setup

1. Clone this repository
2. Install dependencies:
   ```
   pip install flask requests-oauthlib gspread oauth2client python-dotenv
   ```

3. Set up Strava API:
   - Create an application at https://www.strava.com/settings/api
   - Set the Authorization Callback Domain to `localhost`

4. Set up Google Sheets API:
   - Go to Google Cloud Console and create a project
   - Enable Google Sheets API
   - Create a service account and download credentials JSON file
   - Save the JSON file as `gcloud-creds.json` in the project root
   - Create a Google Sheet and share it with the service account email

5. Create a `.env` file based on the `env.example` template:
   ```
   cp env.example .env
   ```
   Then fill in your credentials.

## Running the application

```
python app.py
```

Then visit http://localhost:5000 in your browser.

## How it works

1. Connect your Strava account
2. The app stores your access token and refresh token
3. When you import activities, it automatically refreshes the token if needed
4. Activities are added to your selected Google Sheet

## Managing Spreadsheets

You can manage multiple Google Sheets for different types of activities:

1. Click on "Manage Spreadsheets" from the home screen
2. Add new spreadsheets by providing:
   - The sheet name
   - Either the full Google Sheets URL (e.g., https://docs.google.com/spreadsheets/d/1AIcu4w9Yr3ZURwGrFARZ9ntgjdfeIz9NRCXHXmbfgnY/edit?usp=sharing)
   - Or just the sheet ID
3. Set a default spreadsheet that will be pre-selected when importing activities
4. When importing activities, you can select which spreadsheet to use

### Important: Sharing Your Spreadsheet

For the application to access your Google Sheets, you must share each spreadsheet with the service account email address that appears in the "Add Spreadsheet" form. Follow these steps:

1. Open your Google Sheet
2. Click the "Share" button in the top-right corner
3. Add the service account email with "Editor" access
4. Make sure your spreadsheet has the headers you want to use in the first row:
   - Дата (Date)
   - Разстояние (Distance)
   - Време (Time)
   - Темпо (Pace)
   - Пулс (Heart Rate)

## Importing Activities with Optional Columns

When importing activities, you can choose which data columns to include:

1. After previewing activities, you'll see checkboxes for each data column
2. Select only the columns you want to import
3. The app will only look for and update those specific columns in your spreadsheet
4. This allows you to customize your spreadsheet layout and only include the data you need

## Per-Spreadsheet Column Preferences

The application remembers your column preferences for each spreadsheet:

1. When adding or editing a spreadsheet, you can set default column preferences
2. These preferences will be automatically applied when you select that spreadsheet
3. You can still modify the column selections before importing
4. Your changes will be saved for future imports with that spreadsheet
5. This makes it easy to have different column configurations for different types of activities or different spreadsheets 