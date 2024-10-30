
# GraniteBot

GraniteBot is an internal tool designed for Granite Telecommunications to assist with ticket inquiries, database searches, 
and integration with Microsoft Teams to retrieve relevant chat data for tickets. This bot leverages OpenAI's API, MS Graph API, 
and a SQL database to provide efficient responses and helpful information to internal users.

## Table of Contents
- [Overview](#overview)
- [File Structure](#file-structure)
- [Installation](#installation)
- [Usage](#usage)
- [Environment Variables](#environment-variables)
- [Functions Overview](#functions-overview)
  - [bot.py](#botpy)
  - [MSGraphAuthenticate.py](#msgraphauthenticatepy)
- [Troubleshooting](#troubleshooting)

## Overview
GraniteBot combines ticket data from a SQL database with chat information from MS Teams, using APIs to interactively respond 
to user queries regarding specific tickets or general information requests.

## File Structure
- `bot.py`: Main bot logic handling user input, context detection, ticket information retrieval, and database interaction.
- `MSGraphAuthenticate.py`: Authenticates and searches MS Teams conversations for relevant chat data linked to ticket numbers.

## Installation
1. Clone this repository to your local environment.
2. Install required packages using:
    ```bash
    pip install -r requirements.txt
    ```

## Usage
1. Run `bot.py` in your terminal to start GraniteBot:
    ```bash
    python bot.py
    ```
2. Enter prompts directly into the console to interact with GraniteBot. Example prompts include asking for specific ticket details 
   or querying based on serial numbers or account numbers.

## Environment Variables
Define these in a `.env` file at the project root:
- `OPENAI_API_KEY`: API key for OpenAI.
- `GP_SERVER`, `GP_DATABASE`, `GRT_USER`, `GRT_PASS`: Credentials and connection information for the SQL database. GRT_USER requires server prefix (e.g. GRT0\username)
- `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`: Required for MS Graph API authentication.

## Functions Overview

### bot.py
- **determine_context**: Determines if the user prompt is a general query, ticket-related, or database search.
- **get_ticket_info**: Retrieves ticket details from multiple sources, including the SQL database and MS Teams chat data.
- **execute_query**: Runs SQL queries based on user inputs to retrieve ticket or account details.
- **generate_sql_query**: Creates SQL queries dynamically to satisfy user requests.
- **summarize_chat_data**: Provides summarized information on MS Teams chat data related to tickets.

### MSGraphAuthenticate.py
- **Authenticate**: Handles authentication to MS Graph API using Azure credentials.
- **TeamsSearch**: Retrieves conversations related to a ticket from MS Teams.

## Troubleshooting
- **Token Limit Issues**: If messages exceed token limits, ensure prompt sizes are reduced or adjust `conversation_history` length.
- **MS Teams Data Not Displayed**: Ensure `ms_teams_chat_data` is correctly converted to a string in `get_ticket_info` before use.
- **Permissions Errors**: Verify Azure credentials and permissions in MS Graph API are correctly configured for MS Teams access.

## License
This project is for internal use by Granite Telecommunications and should not be distributed outside the company.
